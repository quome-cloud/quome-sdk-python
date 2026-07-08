from __future__ import annotations

import itertools
import json
from typing import Any

import httpx
import pytest
import respx

from quome import ExecResult, Quome, Sandbox, SandboxProvisioningError

BASE_URL = "https://api.quome.studio"
ORG_ID = "11111111-1111-1111-1111-111111111111"
TEMPLATE_ID = "22222222-2222-2222-2222-222222222222"
SANDBOX_ID = "33333333-3333-3333-3333-333333333333"

SANDBOXES_URL = f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandboxes"
SANDBOX_URL = f"{SANDBOXES_URL}/{SANDBOX_ID}"


@pytest.fixture(autouse=True)
def _org_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every test here is about sandbox behavior, not org resolution — pin it
    # via the env override so tests don't need to also mock api-keys/self.
    monkeypatch.setenv("QUOME_ORG_ID", ORG_ID)


def _client() -> Quome:
    return Quome(api_key="sk_test_key")


def _payload(status: str = "running", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": SANDBOX_ID,
        "status": status,
        "proxy_subdomain": "sbx-abc123",
        "exposed_ports": [],
        "expires_at": "2026-07-08T00:00:00Z",
        "created_at": "2026-07-07T00:00:00Z",
        "started_at": None,
    }
    payload.update(overrides)
    return payload


def _sandbox(status: str = "running", **overrides: Any) -> Sandbox:
    return Sandbox(_client(), _payload(status=status, **overrides))


def _request_json(request: httpx.Request) -> Any:
    return json.loads(request.content)


# --- create --------------------------------------------------------------


@respx.mock
def test_create_resolves_template_name_then_posts() -> None:
    respx.get(f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandbox-templates").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": TEMPLATE_ID,
                    "name": "Code Write",
                    "image": "gcr.io/x",
                    "default_resources": {},
                }
            ],
        )
    )
    create_route = respx.post(SANDBOXES_URL).mock(
        return_value=httpx.Response(201, json=_payload(status="running"))
    )

    # wait mechanics (already-running vs polling vs terminal-fail) are
    # covered separately below — skip waiting here to keep this test
    # focused on template resolution.
    sbx = Sandbox.create(template="Code Write", wait=False, client=_client())

    assert sbx.id == SANDBOX_ID
    assert sbx.status == "running"
    body = _request_json(create_route.calls.last.request)
    assert body["template_id"] == TEMPLATE_ID


@respx.mock
def test_create_uuid_template_skips_resolution() -> None:
    templates_route = respx.get(f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandbox-templates")
    respx.post(SANDBOXES_URL).mock(
        return_value=httpx.Response(201, json=_payload(status="running"))
    )

    Sandbox.create(template=TEMPLATE_ID, wait=False, client=_client())

    assert not templates_route.called


@respx.mock
def test_create_passes_optional_fields() -> None:
    create_route = respx.post(SANDBOXES_URL).mock(
        return_value=httpx.Response(201, json=_payload(status="running"))
    )

    Sandbox.create(
        template=TEMPLATE_ID,
        name="my-sbx",
        resources={"cpu": "2"},
        idle_timeout=900,
        wait=False,
        client=_client(),
    )

    body = _request_json(create_route.calls.last.request)
    assert body == {
        "template_id": TEMPLATE_ID,
        "name": "my-sbx",
        "resources": {"cpu": "2"},
        "idle_timeout": 900,
    }


@respx.mock
def test_create_wait_polls_until_running() -> None:
    respx.post(SANDBOXES_URL).mock(
        return_value=httpx.Response(201, json=_payload(status="provisioning"))
    )
    get_route = respx.get(SANDBOX_URL)
    get_route.side_effect = [
        httpx.Response(200, json=_payload(status="provisioning")),
        httpx.Response(200, json=_payload(status="provisioning")),
        httpx.Response(200, json=_payload(status="running")),
    ]

    sbx = Sandbox.create(template=TEMPLATE_ID, client=_client())

    assert sbx.status == "running"
    assert get_route.call_count == 3


@respx.mock
def test_create_wait_false_never_polls() -> None:
    respx.post(SANDBOXES_URL).mock(
        return_value=httpx.Response(201, json=_payload(status="provisioning"))
    )
    get_route = respx.get(SANDBOX_URL)

    sbx = Sandbox.create(template=TEMPLATE_ID, wait=False, client=_client())

    assert sbx.status == "provisioning"
    assert not get_route.called


@respx.mock
def test_create_wait_raises_on_terminal_failure_not_timeout() -> None:
    respx.post(SANDBOXES_URL).mock(
        return_value=httpx.Response(201, json=_payload(status="provisioning"))
    )
    respx.get(SANDBOX_URL).mock(return_value=httpx.Response(200, json=_payload(status="failed")))

    with pytest.raises(SandboxProvisioningError) as excinfo:
        Sandbox.create(template=TEMPLATE_ID, timeout=300, client=_client())

    assert excinfo.value.status == "failed"
    assert excinfo.value.sandbox_id == SANDBOX_ID


@respx.mock
@pytest.mark.parametrize("terminal_status", ["failed", "error", "deleted"])
def test_create_wait_raises_for_every_terminal_state(terminal_status: str) -> None:
    respx.post(SANDBOXES_URL).mock(
        return_value=httpx.Response(201, json=_payload(status="provisioning"))
    )
    respx.get(SANDBOX_URL).mock(
        return_value=httpx.Response(200, json=_payload(status=terminal_status))
    )

    with pytest.raises(SandboxProvisioningError):
        Sandbox.create(template=TEMPLATE_ID, client=_client())


@respx.mock
def test_create_wait_raises_timeout_error_if_never_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stuck in "provisioning" forever — never terminal-fail, never running —
    # must raise plain TimeoutError, distinct from SandboxProvisioningError.
    respx.post(SANDBOXES_URL).mock(
        return_value=httpx.Response(201, json=_payload(status="provisioning"))
    )
    respx.get(SANDBOX_URL).mock(
        return_value=httpx.Response(200, json=_payload(status="provisioning"))
    )

    # Sleep is already a no-op (conftest); fake time.monotonic() to jump far
    # ahead each call so the deadline trips instantly instead of spinning.
    fake_clock = itertools.count(0, 1000)
    monkeypatch.setattr("quome._poll.time.monotonic", lambda: next(fake_clock))

    with pytest.raises(TimeoutError):
        Sandbox.create(template=TEMPLATE_ID, timeout=300, client=_client())


# --- get / list ------------------------------------------------------------


@respx.mock
def test_get_fetches_single_sandbox() -> None:
    respx.get(SANDBOX_URL).mock(return_value=httpx.Response(200, json=_payload()))

    sbx = Sandbox.get(SANDBOX_ID, client=_client())

    assert sbx.id == SANDBOX_ID


@respx.mock
def test_list_returns_all_sandboxes_from_enveloped_response() -> None:
    respx.get(SANDBOXES_URL).mock(
        return_value=httpx.Response(200, json={"data": [_payload()], "meta": {"total": 1}})
    )

    sandboxes = Sandbox.list(client=_client())

    assert len(sandboxes) == 1
    assert sandboxes[0].id == SANDBOX_ID


@respx.mock
def test_list_returns_all_sandboxes_from_bare_list_response() -> None:
    respx.get(SANDBOXES_URL).mock(return_value=httpx.Response(200, json=[_payload()]))

    sandboxes = Sandbox.list(client=_client())

    assert len(sandboxes) == 1


# --- lifecycle ---------------------------------------------------------


@respx.mock
def test_stop_hits_stop_endpoint_and_updates_status() -> None:
    respx.post(f"{SANDBOX_URL}/stop").mock(
        return_value=httpx.Response(200, json=_payload(status="stopped"))
    )

    sbx = _sandbox()
    sbx.stop()

    assert sbx.status == "stopped"


@respx.mock
def test_resume_hits_resume_endpoint_and_updates_status() -> None:
    respx.post(f"{SANDBOX_URL}/resume").mock(
        return_value=httpx.Response(200, json=_payload(status="running"))
    )

    sbx = _sandbox(status="stopped")
    sbx.resume()

    assert sbx.status == "running"


@respx.mock
def test_delete_hits_delete_endpoint() -> None:
    route = respx.delete(SANDBOX_URL).mock(return_value=httpx.Response(204))

    _sandbox().delete()

    assert route.called


@respx.mock
def test_extend_hits_extend_endpoint_and_updates_expiry() -> None:
    respx.post(f"{SANDBOX_URL}/extend").mock(
        return_value=httpx.Response(200, json=_payload(expires_at="2026-07-09T00:00:00Z"))
    )

    sbx = _sandbox()
    sbx.extend()

    assert sbx.expires_at == "2026-07-09T00:00:00Z"


@respx.mock
def test_refresh_re_gets_and_updates_in_place() -> None:
    respx.get(SANDBOX_URL).mock(return_value=httpx.Response(200, json=_payload(status="running")))

    sbx = _sandbox(status="provisioning")
    result = sbx.refresh()

    assert sbx.status == "running"
    assert result is sbx


# --- run() dispatch matrix ------------------------------------------------


@respx.mock
def test_run_short_timeout_dispatches_single_sync_post() -> None:
    route = respx.post(f"{SANDBOX_URL}/exec").mock(
        return_value=httpx.Response(200, json={"exit_code": 0, "stdout": "hi\n", "stderr": ""})
    )

    result = _sandbox().run("echo hi", timeout=30)

    assert result == ExecResult(exit_code=0, stdout="hi\n", stderr="")
    assert route.call_count == 1
    body = _request_json(route.calls.last.request)
    assert body["mode"] == "sync"
    assert body["command"] == "echo hi"
    assert body["timeout"] == 30


@respx.mock
def test_run_default_timeout_uses_sync_path() -> None:
    # timeout defaults to 300 in the brief's public signature, but the sync
    # ceiling is 240 — the default call still routes through the async job
    # path unless the caller passes a short timeout explicitly.
    route = respx.post(f"{SANDBOX_URL}/exec")
    route.mock(return_value=httpx.Response(200, json={"job_id": "job-default"}))
    respx.get(f"{SANDBOX_URL}/exec/job-default").mock(
        return_value=httpx.Response(
            200, json={"status": "done", "exit_code": 0, "stdout": "", "stderr": ""}
        )
    )

    result = _sandbox().run("echo hi")

    assert result.exit_code == 0
    body = _request_json(route.calls.last.request)
    assert body["mode"] == "async"


@respx.mock
def test_run_long_timeout_submits_async_job_then_polls_to_done() -> None:
    exec_route = respx.post(f"{SANDBOX_URL}/exec").mock(
        return_value=httpx.Response(200, json={"job_id": "job-1"})
    )
    status_route = respx.get(f"{SANDBOX_URL}/exec/job-1")
    status_route.side_effect = [
        httpx.Response(200, json={"status": "running"}),
        httpx.Response(200, json={"status": "running"}),
        httpx.Response(
            200, json={"status": "done", "exit_code": 1, "stdout": "out", "stderr": "err"}
        ),
    ]

    result = _sandbox().run("pytest -q", timeout=1800)

    assert result == ExecResult(exit_code=1, stdout="out", stderr="err")
    body = _request_json(exec_route.calls.last.request)
    assert body["mode"] == "async"
    assert body["timeout"] == 1800
    assert status_route.call_count == 3


@respx.mock
def test_run_async_job_times_out_if_never_done(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post(f"{SANDBOX_URL}/exec").mock(
        return_value=httpx.Response(200, json={"job_id": "job-stuck"})
    )
    respx.get(f"{SANDBOX_URL}/exec/job-stuck").mock(
        return_value=httpx.Response(200, json={"status": "running"})
    )

    # Sleep is already a no-op (conftest), so real wall-clock time barely
    # advances between polls — fake time.monotonic() to jump far ahead each
    # call so the deadline actually trips instead of spinning for 241s.
    fake_clock = itertools.count(0, 1000)
    monkeypatch.setattr("quome._poll.time.monotonic", lambda: next(fake_clock))

    with pytest.raises(TimeoutError):
        _sandbox().run("sleep 9999", timeout=241.01)


def test_run_with_on_stdout_dispatches_to_stream_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unit-test the dispatch only — the real WS protocol (ticket + frames)
    # is covered end-to-end against a local server in test_stream.py.
    calls: list[tuple[Any, ...]] = []

    def _fake_stream_exec(
        transport: Any,
        org_id: Any,
        sandbox_id: Any,
        command: Any,
        working_dir: Any,
        env: Any,
        on_stdout: Any,
        timeout: Any,
    ) -> ExecResult:
        calls.append((org_id, sandbox_id, command, working_dir, env, timeout))
        on_stdout("chunk")
        return ExecResult(exit_code=None, stdout="chunk", stderr="")

    monkeypatch.setattr("quome.sandbox.stream_exec", _fake_stream_exec)

    received: list[str] = []
    result = _sandbox().run("tail -f log", on_stdout=received.append)

    assert result == ExecResult(exit_code=None, stdout="chunk", stderr="")
    assert received == ["chunk"]
    assert calls == [(ORG_ID, SANDBOX_ID, "tail -f log", "/workspace", None, 300)]


def test_run_with_on_stdout_ignores_timeout_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    # on_stdout takes priority over the sync/async timeout split — even a
    # short timeout should route to the streaming path, not sync exec.
    calls: list[Any] = []
    monkeypatch.setattr(
        "quome.sandbox.stream_exec",
        lambda *args, **kwargs: (
            calls.append(args) or ExecResult(exit_code=None, stdout="", stderr="")
        ),
    )

    _sandbox().run("echo hi", timeout=5, on_stdout=lambda _line: None)

    assert len(calls) == 1


# --- preview_url ---------------------------------------------------------


def test_preview_url_requires_public_kwarg() -> None:
    with pytest.raises(TypeError):
        _sandbox().preview_url(8000)  # type: ignore[call-arg]


def test_preview_url_public_false_raises_value_error() -> None:
    with pytest.raises(ValueError, match="internet-reachable"):
        _sandbox().preview_url(8000, public=False)


@respx.mock
def test_preview_url_public_true_posts_ports_and_returns_server_url() -> None:
    route = respx.post(f"{SANDBOX_URL}/ports").mock(
        return_value=httpx.Response(
            200, json={"port": 8000, "url": "https://sbx-abc123-8000.acme.quome.dev"}
        )
    )

    url = _sandbox().preview_url(8000, public=True)

    assert url == "https://sbx-abc123-8000.acme.quome.dev"
    body = _request_json(route.calls.last.request)
    assert body == {"port": 8000}


def test_preview_url_docstring_states_internet_reachable() -> None:
    doc = Sandbox.preview_url.__doc__ or ""
    assert "internet-reachable" in doc
