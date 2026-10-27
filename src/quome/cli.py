"""``quome-workflow`` command line: run DOH workflow specs in a Quome sandbox.

Subcommands::

    quome-workflow workflow templates
    quome-workflow workflow ensure-template --name N --image I [...]
    quome-workflow workflow run SPEC_DIR_OR_FILE [--inputs JSON|FILE] [...]

This is a port of the reference driver in DOH (``doh_flow.sandbox``). The
runner package itself is *not* vendored: pass ``--runner-tarball`` pointing at
a ``doh_flow.tar.gz`` built from DOH (see README, "Workflows").

Exit codes: 0 completed, 1 run failed/timed out/unreachable, 2 usage error or
spec/template/secret/tarball not found, 3 missing Quome API key or Claude
token, 4 provisioning/bootstrap/run-start failure, 5 teardown failed (the
sandbox id is printed; it may be leaked).

The Claude token is only ever passed as a process ``env=`` to the detached
run; it is never logged (all output is scrubbed of it).
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shlex
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .client import Quome
from .sandbox import Sandbox
from .templates import list_templates, resolve_template

DEFAULT_TEMPLATE = "claude-workflow-runner"
DEST = "/workspace/run"
OUT = DEST + "/out"
SENTINEL = "__DOH_FLOW_RESULT__"
MAX_POLL_FAILURES = 6
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
_SKIP_DIRS = {"__pycache__", ".git"}
_SPEC_NAMES = ("workflow.yaml", "workflow.yml", "workflow.json", "spec.yaml", "spec.yml")
_SIDE_DIRS = ("prompts", "schemas", "steps")


class _SandboxLeaked(Exception):
    def __init__(self, sandbox_id: str | None, why: str = "") -> None:
        super().__init__(f"sandbox {sandbox_id} may be leaked" + (f": {why}" if why else ""))
        self.sandbox_id = sandbox_id


@dataclass
class RunResult:
    exit_code: int
    status: str
    reason: str = ""
    sandbox_id: str | None = None
    result: dict[str, Any] | None = None
    artifacts: list[str] = field(default_factory=list)


def resolve_token(
    environ: dict[str, str] | None = None, creds_path: str | None = None
) -> str | None:
    """``$CLAUDE_CODE_OAUTH_TOKEN``, else ``~/.claude/.credentials.json`` accessToken."""
    tok = (environ if environ is not None else os.environ).get(TOKEN_ENV)
    if tok:
        return tok
    path = creds_path or os.path.expanduser("~/.claude/.credentials.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return str((data.get("claudeAiOauth") or {}).get("accessToken") or "") or None
    except (OSError, ValueError, AttributeError):
        return None


def parse_secrets(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        name, sep, ref = item.partition("=")
        if not sep or not name or not ref:
            raise ValueError(f"--secret expects NAME=SECRET_ID (got {item!r})")
        out[name] = ref
    return out


def _load_inputs(arg: str | None) -> dict[str, Any]:
    if not arg:
        return {}
    if arg.lstrip().startswith("{"):
        text = arg
    else:
        with open(arg, encoding="utf-8") as f:
            text = f.read()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("inputs must be a JSON object")
    return data


def _walk(base: str, sub: str = "") -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    root_dir = os.path.join(base, sub) if sub else base
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for fn in sorted(files):
            if fn.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            with open(full, "rb") as f:
                out.append((rel, f.read()))
    return out


def build_payload(
    spec: str, inputs: dict[str, Any], runner_tarball: bytes
) -> list[tuple[str, bytes]]:
    """Return ``[(relative_path, bytes)]``; the spec file is always first.

    ``spec`` is a spec file (shipped with sibling ``prompts/ schemas/ steps/``)
    or a directory (shipped whole; the spec is the first of ``workflow.yaml``,
    ``workflow.yml``, ``workflow.json``, ``spec.yaml``, ``spec.yml``, or the
    only ``*.yaml/yml/json`` file in it). Raises ``OSError`` if unreadable.
    """
    files: list[tuple[str, bytes]]
    if os.path.isdir(spec):
        base = os.path.abspath(spec)
        names = sorted(n for n in os.listdir(base) if n.endswith((".yaml", ".yml", ".json")))
        spec_name = next((n for n in _SPEC_NAMES if n in names), None)
        if spec_name is None and len(names) == 1:
            spec_name = names[0]
        if spec_name is None:
            raise OSError(f"no workflow spec found in {spec} (expected workflow.yaml)")
        files = _walk(base)
        files.sort(key=lambda p: p[0] != spec_name)
    else:
        base = os.path.dirname(os.path.abspath(spec))
        with open(spec, "rb") as f:
            files = [(os.path.basename(spec), f.read())]
        for sub in _SIDE_DIRS:
            files.extend(_walk(base, sub))
    files = [p for p in files if p[0] not in ("inputs.json", "doh_flow.tar.gz")]
    files.append(("inputs.json", json.dumps(inputs or {}, sort_keys=True).encode("utf-8")))
    files.append(("doh_flow.tar.gz", runner_tarball))
    return files


def _split_poll(stdout: str | None) -> tuple[str, dict[str, Any] | None]:
    """poll output is '<log tail>\\n<SENTINEL>\\n<result.json or empty>'."""
    log, sep, tail = (stdout or "").rpartition(SENTINEL)
    if not sep:
        return stdout or "", None
    tail = tail.strip()
    if not tail:
        return log.rstrip("\n"), None
    try:
        data = json.loads(tail)
    except ValueError:
        return log.rstrip("\n"), None  # partially written; next poll
    ok = isinstance(data, dict) and data.get("status")
    return log.rstrip("\n"), data if ok else None


def _safe_rel(rel: str) -> str | None:
    rel = posixpath.normpath(rel)
    return None if rel.startswith("..") or posixpath.isabs(rel) else rel


def _write_local(path: str, data: bytes | str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else str(data).encode("utf-8"))


def _collect(sbx: Sandbox, out_dir: str, say: Callable[[str], None]) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    got: list[str] = []
    try:
        _write_local(os.path.join(out_dir, "result.json"), sbx.files.read(OUT + "/result.json"))
        got.append("result.json")
    except Exception as e:  # noqa: BLE001
        say(f"collect: result.json unreadable ({type(e).__name__})")
    try:
        listing = sbx.run(f"cd {OUT} && find artifacts -type f 2>/dev/null", timeout=30).stdout
    except Exception as e:  # noqa: BLE001
        say(f"collect: artifact listing failed ({type(e).__name__})")
        listing = ""
    for line in (listing or "").splitlines():
        rel_s = line.strip()
        if rel_s.startswith(OUT + "/"):
            rel_s = rel_s[len(OUT) + 1 :]
        rel = _safe_rel(rel_s) if rel_s else None
        if not rel:
            continue
        try:
            data = sbx.files.read(OUT + "/" + rel)
        except Exception as e:  # noqa: BLE001
            say(f"collect: {rel} failed ({type(e).__name__})")
            continue
        _write_local(os.path.join(out_dir, *rel.split("/")), data)
        got.append(rel)
    return got


def _exit_for(e: Exception) -> int:
    code = getattr(e, "status_code", None)
    name = type(e).__name__.lower()
    if code in (401, 403, 404) or any(w in name for w in ("auth", "notfound", "permission")):
        return 2
    return 4


def _create_sandbox(client: Quome, template_id: str, name: str) -> Sandbox:
    try:
        return Sandbox.create(
            template_id, name=name, idle_timeout=1800, wait=True, timeout=300, client=client
        )
    except Exception as e:  # noqa: BLE001 - reap a half-created sandbox
        sid = getattr(e, "sandbox_id", None)
        try:
            if sid:
                Sandbox.get(sid, client=client).delete()
            else:
                for sb in Sandbox.list(client=client):
                    if sb._data.get("name") == name:
                        sid = sb.id
                        sb.delete()
        except Exception as e2:  # noqa: BLE001
            raise _SandboxLeaked(sid or name, type(e2).__name__) from e
        raise


def run_workflow(
    spec: str,
    inputs: dict[str, Any],
    *,
    client: Quome,
    runner_tarball: bytes,
    token: str,
    template: str = DEFAULT_TEMPLATE,
    out_dir: str = "./out-sandbox",
    timeout_s: int = 2700,
    keep_on_failure: bool = False,
    secrets: dict[str, str] | None = None,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    """create, ship, bootstrap, inject secrets, detached run, poll, collect, delete."""

    def say(msg: object) -> None:
        log(str(msg).replace(token, "***") if token else str(msg))

    try:
        payload = build_payload(spec, inputs, runner_tarball)
    except OSError as e:
        say(f"cannot read spec: {e}")
        return RunResult(2, "failed", "spec unreadable")
    try:
        template_id = resolve_template(client.transport, client.org_id, template)
    except Exception as e:  # noqa: BLE001
        say(f"template lookup failed: {type(e).__name__}: {e}")
        return RunResult(_exit_for(e), "failed", "template lookup failed")

    state: dict[str, Any] = {"sbx": None, "failed": True}
    res: RunResult | None = None
    try:
        res = _run_body(
            client,
            payload,
            template_id,
            state,
            secrets or {},
            token,
            out_dir,
            timeout_s,
            say,
            sleep,
        )
        state["failed"] = res.exit_code != 0
    finally:
        sbx = state["sbx"]
        leaked = False
        if sbx is not None:
            if keep_on_failure and state["failed"]:
                say(f"--keep-on-failure: leaving sandbox {sbx.id} running (delete it yourself)")
            else:
                try:
                    sbx.delete()
                except Exception as e:  # noqa: BLE001
                    leaked = True
                    say(f"delete of {sbx.id} failed ({type(e).__name__}); trying retry-delete")
                    try:
                        client.transport.request(
                            "POST", f"/api/v1/orgs/{client.org_id}/sandboxes/{sbx.id}/retry-delete"
                        )
                        leaked = False
                    except Exception as e2:  # noqa: BLE001
                        say(f"retry-delete failed ({type(e2).__name__})")
                    if leaked:
                        say(f"SANDBOX {sbx.id} MAY BE LEAKED - verify and delete it manually")
    if res is not None and leaked:
        res.exit_code = 5
    assert res is not None
    return res


def _run_body(
    client: Quome,
    payload: list[tuple[str, bytes]],
    template_id: str,
    state: dict[str, Any],
    secrets: dict[str, str],
    token: str,
    out_dir: str,
    timeout_s: int,
    say: Callable[[str], None],
    sleep: Callable[[float], None],
) -> RunResult:
    try:
        sbx = _create_sandbox(client, template_id, "doh-flow-" + uuid.uuid4().hex[:10])
        state["sbx"] = sbx
    except _SandboxLeaked as e:
        say(
            f"provisioning failed and cleanup failed: {e} -- delete sandbox {e.sandbox_id} manually"
        )
        return RunResult(5, "failed", "provisioning leaked", e.sandbox_id)
    except Exception as e:  # noqa: BLE001
        say(f"provisioning failed: {type(e).__name__}: {e}")
        return RunResult(4, "failed", "provisioning failed")
    sid = sbx.id
    say(f"sandbox {sid} created")
    try:
        for rel, data in payload:
            sbx.files.write(posixpath.join(DEST, rel), data)
        boot = sbx.run(
            f"mkdir -p /opt/doh_flow && tar xzf {DEST}/doh_flow.tar.gz -C /opt/doh_flow && "
            "PYTHONPATH=/opt/doh_flow python3 -m doh_flow --version",
            timeout=120,
        )
        if boot.exit_code != 0 or not (boot.stdout or "").strip():
            say(
                f"bootstrap failed (exit {boot.exit_code}): "
                f"{(boot.stderr or boot.stdout or '')[-300:]}"
            )
            return RunResult(4, "failed", "bootstrap failed", sid)
        for name, sec in secrets.items():
            client.transport.request(
                "POST",
                f"/api/v1/orgs/{client.org_id}/sandboxes/{sid}/inject-secret",
                json={"secret_id": sec},
            )
            say(f"injected org secret for {name}")
        detached = (
            f"cd {shlex.quote(DEST)} && nohup env PYTHONPATH=/opt/doh_flow python3 -m doh_flow run "
            f"{shlex.quote(payload[0][0])} --inputs inputs.json --out {shlex.quote(OUT)} "
            "> run.log 2>&1 & echo $!"
        )
        started = sbx.run(detached, env={TOKEN_ENV: token}, timeout=30)
        if started.exit_code != 0:
            say(
                f"detached start failed (exit {started.exit_code}): {(started.stderr or '')[-300:]}"
            )
            return RunResult(4, "failed", "run start failed", sid)
    except Exception as e:  # noqa: BLE001
        say(f"setup failed: {type(e).__name__}: {e}")
        return RunResult(4, "failed", "setup failed", sid)
    say(f"run started (pid {(started.stdout or '').strip()}); polling")

    poll_cmd = (
        f"tail -c 4000 {shlex.quote(DEST)}/run.log; printf '\\n{SENTINEL}\\n'; "
        f"cat {shlex.quote(OUT)}/result.json 2>/dev/null"
    )
    fails = 0
    t_poll = time.monotonic()
    last_log = ""
    status, reason = "failed", ""
    result: dict[str, Any] | None = None
    while True:
        try:
            try:
                r = sbx.run(poll_cmd, timeout=30)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001 - one transient retry per poll
                say(f"transient poll error ({type(e).__name__}); retrying once")
                r = sbx.run(poll_cmd, timeout=30)
            text, result = _split_poll(r.stdout)
            if text and text != last_log:
                say((text[len(last_log) :] if text.startswith(last_log) else text).rstrip("\n"))
                last_log = text
            fails = 0
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            say(f"poll failed twice ({type(e).__name__}: {e})")
            result = None
            fails += 1
            if fails >= MAX_POLL_FAILURES:
                say(f"sandbox unreachable after {fails} consecutive poll failures")
                return RunResult(1, "failed", "sandbox_unreachable", sid)
        if result is not None:
            break
        elapsed = time.monotonic() - t_poll
        if elapsed >= timeout_s:
            reason = "timeout"
            say(f"timeout after {timeout_s}s; last log:\n{last_log[-1000:]}")
            break
        sleep(5 if elapsed < 60 else 10)

    artifacts = _collect(sbx, out_dir, say)
    exit_code = 1
    if result is not None:
        status, reason = str(result.get("status", "failed")), str(result.get("reason") or "")
        exit_code = 0 if status == "completed" else 1
    return RunResult(exit_code, status, reason, sid, result, artifacts)


# -- subcommands ---------------------------------------------------------------


def cmd_templates(client: Quome) -> int:
    for t in list_templates(client.transport, client.org_id):
        print(f"{t.name}\t{t.id}\t{t.image}")
    return 0


def ensure_template(client: Quome, name: str, image: str, **fields: Any) -> dict[str, Any]:
    """Idempotent: return the existing template called ``name`` or create it."""
    for t in list_templates(client.transport, client.org_id):
        if t.name.lower() == name.lower():
            return {"id": t.id, "name": t.name, "image": t.image, "created": False}
    body: dict[str, Any] = {"name": name, "image": image, "image_source": "quome"}
    body.update({k: v for k, v in fields.items() if v is not None})
    resp = client.transport.request(
        "POST", f"/api/v1/orgs/{client.org_id}/sandbox-templates", json=body
    )
    made = resp.json()
    made = made if isinstance(made, dict) else {}
    return {
        "id": made.get("id"),
        "name": made.get("name", name),
        "image": made.get("image", image),
        "created": True,
    }


def _resolve_secret_id(client: Quome, ref: str) -> str:
    try:
        uuid.UUID(ref)
        return ref
    except ValueError:
        pass
    resp = client.transport.request("GET", f"/api/v1/orgs/{client.org_id}/secrets")
    payload = resp.json()
    items = payload.get("data", []) if isinstance(payload, dict) else list(payload or [])
    for sec in items:
        if str(sec.get("name", "")).lower() == ref.lower():
            return str(sec["id"])
    raise KeyError(f"no org secret named {ref!r}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="quome-workflow")
    top = ap.add_subparsers(dest="group", required=True)
    wf = top.add_parser("workflow", help="run workflow specs in a Quome sandbox")
    sub = wf.add_subparsers(dest="cmd", required=True)
    sub.add_parser("templates", help="list sandbox templates")
    et = sub.add_parser("ensure-template", help="create the runner template if missing")
    et.add_argument("--name", default=DEFAULT_TEMPLATE)
    et.add_argument("--image", required=True)
    et.add_argument("--run-as-user")
    et.add_argument("--max-lifetime", type=int)
    et.add_argument("--idle-timeout", type=int)
    r = sub.add_parser("run", help="run a workflow spec in a fresh sandbox")
    r.add_argument("spec", metavar="SPEC_DIR_OR_FILE")
    r.add_argument("--inputs", help="JSON object or path to a JSON file")
    r.add_argument("--template", default=DEFAULT_TEMPLATE)
    r.add_argument("--out", default="./out-sandbox")
    r.add_argument("--timeout", type=int, default=2700)
    r.add_argument("--keep-on-failure", action="store_true")
    r.add_argument("--runner-tarball", default="./doh_flow.tar.gz")
    r.add_argument("--secret", action="append", default=[], metavar="NAME=SECRET_ID")
    return ap


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        try:
            inputs = _load_inputs(args.inputs)
            secrets = parse_secrets(args.secret)
        except (OSError, ValueError) as e:
            _err(f"bad argument: {e}")
            return 2
        if not os.path.exists(args.spec):
            _err(f"spec not found: {args.spec}")
            return 2
        try:
            with open(args.runner_tarball, "rb") as f:
                tarball = f.read()
        except OSError:
            _err(
                f"runner tarball not found: {args.runner_tarball} (build it from DOH: "
                "tar czf doh_flow.tar.gz -C bin/lib doh_flow event_watcher_template.py)"
            )
            return 2
    if not os.environ.get("QUOME_API_KEY"):
        _err("QUOME_API_KEY is not set")
        return 3
    client = Quome()
    if args.cmd == "templates":
        return cmd_templates(client)
    if args.cmd == "ensure-template":
        t = ensure_template(
            client,
            args.name,
            args.image,
            run_as_user=args.run_as_user,
            max_lifetime=args.max_lifetime,
            idle_timeout=args.idle_timeout,
        )
        verb = "created" if t["created"] else "exists"
        print(f"template={t['name']} id={t['id']} image={t['image']} ({verb})")
        return 0
    token = resolve_token()
    if not token:
        _err(f"no Claude token: set ${TOKEN_ENV} or log in with `claude`")
        return 3
    try:
        secrets = {k: _resolve_secret_id(client, v) for k, v in secrets.items()}
    except KeyError as e:
        _err(str(e.args[0]))
        return 2
    except Exception as e:  # noqa: BLE001
        _err(f"secret lookup failed: {type(e).__name__}: {e}")
        return 2
    res = run_workflow(
        args.spec,
        inputs,
        client=client,
        runner_tarball=tarball,
        token=token,
        template=args.template,
        out_dir=args.out,
        timeout_s=args.timeout,
        keep_on_failure=args.keep_on_failure,
        secrets=secrets,
        log=lambda m: print(m, file=sys.stderr),
    )
    print(f"sandbox={res.sandbox_id} status={res.status} exit={res.exit_code} out={args.out}")
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
