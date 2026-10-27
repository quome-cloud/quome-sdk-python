from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from quome import cli

TOKEN = "tok-secret-123"


class FakeFiles:
    def __init__(self, calls: list[Any], store: dict[str, bytes]) -> None:
        self.calls, self.store = calls, store

    def write(self, path: str, content: Any) -> None:
        self.calls.append(("write", path))

    def read(self, path: str) -> bytes:
        self.calls.append(("read", path))
        return self.store[path]


class FakeSandbox:
    last: FakeSandbox | None = None
    delete_raises = False
    run_hook: Any = None

    def __init__(self, calls: list[Any]) -> None:
        self.id = "sbx-1"
        self.calls = calls
        self.store = {
            cli.OUT + "/result.json": b'{"status": "completed"}',
            cli.OUT + "/artifacts/a.txt": b"hi",
        }
        self.files = FakeFiles(calls, self.store)
        self._data = {"name": "x"}
        self.result = {"status": "completed", "run_id": "r1"}

    def run(self, cmd: str, **kw: Any) -> SimpleNamespace:
        self.calls.append(("run", cmd.split()[0] + " " + cmd.split()[1] if " " in cmd else cmd, kw))
        if FakeSandbox.run_hook:
            FakeSandbox.run_hook(cmd)
        if "--version" in cmd:
            return SimpleNamespace(exit_code=0, stdout="doh-flow 0.1", stderr="")
        if "nohup" in cmd:
            return SimpleNamespace(exit_code=0, stdout="42", stderr="")
        if cli.SENTINEL in cmd:
            body = json.dumps(self.result)
            return SimpleNamespace(exit_code=0, stdout=f"log\n{cli.SENTINEL}\n{body}", stderr="")
        if "find artifacts" in cmd:
            return SimpleNamespace(exit_code=0, stdout="artifacts/a.txt\n../evil\n", stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    def delete(self) -> None:
        self.calls.append(("delete",))
        if FakeSandbox.delete_raises:
            raise RuntimeError("boom")


class FakeTransport:
    def __init__(self, calls: list[Any]) -> None:
        self.calls = calls

    def request(self, method: str, path: str, **kw: Any) -> Any:
        self.calls.append(("req", method, path))
        return SimpleNamespace(json=lambda: {})


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> SimpleNamespace:
    calls: list[Any] = []
    FakeSandbox.delete_raises = False
    FakeSandbox.run_hook = None

    def create(template: str, **kw: Any) -> FakeSandbox:
        calls.append(("create", template, kw["name"]))
        FakeSandbox.last = FakeSandbox(calls)
        return FakeSandbox.last

    monkeypatch.setattr(cli.Sandbox, "create", staticmethod(create))
    monkeypatch.setattr(cli, "resolve_template", lambda t, o, n: "tpl-id")
    client = SimpleNamespace(transport=FakeTransport(calls), org_id="org")
    spec = tmp_path / "wf"
    (spec / "prompts").mkdir(parents=True)
    (spec / "workflow.yaml").write_text("name: x\n")
    (spec / "prompts" / "p.md").write_text("hi")
    return SimpleNamespace(calls=calls, client=client, spec=str(spec), out=str(tmp_path / "out"))


def _run(env: SimpleNamespace, **kw: Any) -> cli.RunResult:
    logs: list[str] = []
    res = cli.run_workflow(
        env.spec,
        {"a": 1},
        client=env.client,
        runner_tarball=b"tar",
        token=TOKEN,
        out_dir=env.out,
        log=logs.append,
        sleep=lambda s: None,
        **kw,
    )
    env.logs = logs
    return res


def test_happy_path_order_and_token_hygiene(env: SimpleNamespace) -> None:
    res = _run(env)
    assert res.exit_code == 0 and res.status == "completed"
    kinds = [c[0] for c in env.calls]
    assert kinds[0] == "create" and kinds[-1] == "delete"
    writes = [c[1] for c in env.calls if c[0] == "write"]
    assert writes[0] == cli.DEST + "/workflow.yaml"
    assert cli.DEST + "/inputs.json" in writes and cli.DEST + "/doh_flow.tar.gz" in writes
    assert kinds.index("write") < kinds.index("run")
    starts = [c for c in env.calls if c[0] == "run" and c[2].get("env")]
    assert len(starts) == 1 and starts[0][2]["env"] == {cli.TOKEN_ENV: TOKEN}
    assert os.path.exists(os.path.join(env.out, "result.json"))
    assert os.path.exists(os.path.join(env.out, "artifacts", "a.txt"))
    assert not os.path.exists(os.path.join(os.path.dirname(env.out), "evil"))
    assert all(TOKEN not in line for line in env.logs)


def test_failed_run_exit_1_deletes(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    def hook(cmd: str) -> None:
        if FakeSandbox.last:
            FakeSandbox.last.result = {"status": "failed", "reason": "bad"}

    FakeSandbox.run_hook = hook
    res = _run(env)
    assert res.exit_code == 1 and res.reason == "bad"
    assert env.calls[-1] == ("delete",)


def test_keep_on_failure(env: SimpleNamespace) -> None:
    def hook(cmd: str) -> None:
        if FakeSandbox.last:
            FakeSandbox.last.result = {"status": "failed"}

    FakeSandbox.run_hook = hook
    res = _run(env, keep_on_failure=True)
    assert res.exit_code == 1 and ("delete",) not in env.calls
    FakeSandbox.run_hook = None
    env.calls.clear()
    assert _run(env, keep_on_failure=True).exit_code == 0
    assert ("delete",) in env.calls  # kept only on failure


def test_keyboard_interrupt_deletes(env: SimpleNamespace) -> None:
    def hook(cmd: str) -> None:
        if cli.SENTINEL in cmd:
            raise KeyboardInterrupt

    FakeSandbox.run_hook = hook
    with pytest.raises(KeyboardInterrupt):
        _run(env)
    assert env.calls[-1] == ("delete",)


def test_delete_failure_exit_5(env: SimpleNamespace) -> None:
    FakeSandbox.delete_raises = True

    def bad(*a: Any, **k: Any) -> Any:
        raise RuntimeError("nope")

    env.client.transport.request = bad
    assert _run(env).exit_code == 5


def test_missing_spec_exit_2(env: SimpleNamespace) -> None:
    env.spec = env.spec + "-missing"
    assert _run(env).exit_code == 2
    assert not any(c[0] == "create" for c in env.calls)


def test_main_missing_key_exit_3(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    spec = tmp_path / "w.yaml"
    spec.write_text("x")
    tar = tmp_path / "t.tgz"
    tar.write_bytes(b"x")
    assert cli.main(["workflow", "run", str(spec), "--runner-tarball", str(tar)]) == 3


def test_main_missing_token_exit_3(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("QUOME_API_KEY", "k")
    monkeypatch.delenv(cli.TOKEN_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = tmp_path / "w.yaml"
    spec.write_text("x")
    tar = tmp_path / "t.tgz"
    tar.write_bytes(b"x")
    assert cli.main(["workflow", "run", str(spec), "--runner-tarball", str(tar)]) == 3


def test_main_missing_tarball_exit_2(tmp_path: Any) -> None:
    spec = tmp_path / "w.yaml"
    spec.write_text("x")
    code = cli.main(["workflow", "run", str(spec), "--runner-tarball", str(tmp_path / "no.tgz")])
    assert code == 2


def test_resolve_token_sources(tmp_path: Any) -> None:
    creds = tmp_path / "c.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "from-file"}}))
    assert cli.resolve_token({cli.TOKEN_ENV: "env"}, str(creds)) == "env"
    assert cli.resolve_token({}, str(creds)) == "from-file"
    assert cli.resolve_token({}, str(tmp_path / "none")) is None


def test_parse_secrets() -> None:
    assert cli.parse_secrets(["A=1"]) == {"A": "1"}
    with pytest.raises(ValueError):
        cli.parse_secrets(["bad"])


def test_ensure_template_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    client = SimpleNamespace(transport=FakeTransport(calls), org_id="org")
    existing = [SimpleNamespace(id="i", name="T", image="img", default_resources={})]
    monkeypatch.setattr(cli, "list_templates", lambda t, o: existing)
    out = cli.ensure_template(client, "t", "img")  # type: ignore[arg-type]
    assert out["created"] is False and not calls
    monkeypatch.setattr(cli, "list_templates", lambda t, o: [])
    out = cli.ensure_template(client, "t", "img", max_lifetime=60)  # type: ignore[arg-type]
    assert out["created"] is True and calls == [
        ("req", "POST", "/api/v1/orgs/org/sandbox-templates")
    ]
