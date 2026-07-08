"""Happy-path tests for the async surface (``AsyncQuome`` / ``AsyncSandbox``).

Mirrors a slice of ``test_sandbox.py`` / ``test_files.py`` /
``test_org_and_templates.py`` closely enough to prove the async surface
behaves the same as the sync one against the same wire contract — full
coverage of every branch (retry mechanics, terminal-fail states, timeout
edge cases, ...) already lives in those sync test modules and isn't
duplicated here; ``test_parity.py`` is what guarantees the two surfaces stay
structurally in lockstep.

respx intercepts httpx at the transport level regardless of whether the
client is ``httpx.Client`` or ``httpx.AsyncClient`` — see
``test_transport.py`` for the precedent of mixing ``@respx.mock`` with
``async def`` tests. ``asyncio_mode = "auto"`` (pyproject.toml) means no
``@pytest.mark.asyncio`` decorator is needed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from quome import AsyncQuome, AsyncSandbox, ExecResult

BASE_URL = "https://api.quome.studio"
ORG_ID = "11111111-1111-1111-1111-111111111111"
TEMPLATE_ID = "22222222-2222-2222-2222-222222222222"
SANDBOX_ID = "33333333-3333-3333-3333-333333333333"

SANDBOXES_URL = f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandboxes"
SANDBOX_URL = f"{SANDBOXES_URL}/{SANDBOX_ID}"


@pytest.fixture(autouse=True)
def _org_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests are about the async surface's behavior, not org
    # resolution (that's covered by test_org_and_templates.py) — pin it via
    # the env override so tests don't also need to mock api-keys/self.
    monkeypatch.setenv("QUOME_ORG_ID", ORG_ID)


def _client() -> AsyncQuome:
    return AsyncQuome(api_key="sk_test_key")


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


def _sandbox(status: str = "running", **overrides: Any) -> AsyncSandbox:
    return AsyncSandbox(_client(), _payload(status=status, **overrides))


# --- AsyncQuome.org_id -----------------------------------------------------


async def test_org_id_resolves_from_env_without_http_call() -> None:
    client = _client()

    with respx.mock:
        # No routes registered — any HTTP call would raise inside respx.mock.
        org_id = await client.org_id()

    assert org_id == ORG_ID


async def test_org_id_is_memoized_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUOME_ORG_ID", raising=False)
    client = _client()

    with respx.mock:
        route = respx.get(f"{BASE_URL}/api/v1/api-keys/self").mock(
            return_value=httpx.Response(
                200, json={"org_id": ORG_ID, "service_account_id": None, "scopes": ["*"]}
            )
        )
        first = await client.org_id()
        second = await client.org_id()

    assert first == ORG_ID
    assert second == ORG_ID
    assert route.call_count == 1


# --- AsyncSandbox.create / get ---------------------------------------------


@respx.mock
async def test_create_resolves_template_name_then_posts() -> None:
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

    sbx = await AsyncSandbox.create(template="Code Write", wait=False, client=_client())

    assert sbx.id == SANDBOX_ID
    assert sbx.status == "running"
    body = create_route.calls.last.request.content
    assert TEMPLATE_ID.encode() in body


@respx.mock
async def test_create_uuid_template_skips_resolution() -> None:
    templates_route = respx.get(f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandbox-templates")
    respx.post(SANDBOXES_URL).mock(
        return_value=httpx.Response(201, json=_payload(status="running"))
    )

    await AsyncSandbox.create(template=TEMPLATE_ID, wait=False, client=_client())

    assert not templates_route.called


@respx.mock
async def test_create_wait_polls_until_running() -> None:
    respx.post(SANDBOXES_URL).mock(
        return_value=httpx.Response(201, json=_payload(status="provisioning"))
    )
    get_route = respx.get(SANDBOX_URL)
    get_route.side_effect = [
        httpx.Response(200, json=_payload(status="provisioning")),
        httpx.Response(200, json=_payload(status="running")),
    ]

    sbx = await AsyncSandbox.create(template=TEMPLATE_ID, client=_client())

    assert sbx.status == "running"
    assert get_route.call_count == 2


@respx.mock
async def test_get_fetches_single_sandbox() -> None:
    respx.get(SANDBOX_URL).mock(return_value=httpx.Response(200, json=_payload()))

    sbx = await AsyncSandbox.get(SANDBOX_ID, client=_client())

    assert sbx.id == SANDBOX_ID


@respx.mock
async def test_list_returns_all_sandboxes() -> None:
    respx.get(SANDBOXES_URL).mock(
        return_value=httpx.Response(200, json={"data": [_payload()], "meta": {"total": 1}})
    )

    sandboxes = await AsyncSandbox.list(client=_client())

    assert len(sandboxes) == 1
    assert sandboxes[0].id == SANDBOX_ID


# --- lifecycle ---------------------------------------------------------


@respx.mock
async def test_stop_and_resume_update_status() -> None:
    respx.post(f"{SANDBOX_URL}/stop").mock(
        return_value=httpx.Response(200, json=_payload(status="stopped"))
    )
    respx.post(f"{SANDBOX_URL}/resume").mock(
        return_value=httpx.Response(200, json=_payload(status="running"))
    )

    sbx = _sandbox()
    await sbx.stop()
    assert sbx.status == "stopped"

    await sbx.resume()
    assert sbx.status == "running"


@respx.mock
async def test_delete_hits_delete_endpoint() -> None:
    route = respx.delete(SANDBOX_URL).mock(return_value=httpx.Response(204))

    await _sandbox().delete()

    assert route.called


# --- run() sync-mode dispatch -----------------------------------------------


@respx.mock
async def test_run_short_timeout_dispatches_single_sync_post() -> None:
    route = respx.post(f"{SANDBOX_URL}/exec").mock(
        return_value=httpx.Response(200, json={"exit_code": 0, "stdout": "hi\n", "stderr": ""})
    )

    result = await _sandbox().run("echo hi", timeout=30)

    assert result == ExecResult(exit_code=0, stdout="hi\n", stderr="")
    assert route.call_count == 1


@respx.mock
async def test_run_long_timeout_submits_async_job_then_polls_to_done() -> None:
    exec_route = respx.post(f"{SANDBOX_URL}/exec").mock(
        return_value=httpx.Response(200, json={"job_id": "job-1"})
    )
    status_route = respx.get(f"{SANDBOX_URL}/exec/job-1")
    status_route.side_effect = [
        httpx.Response(200, json={"status": "running"}),
        httpx.Response(
            200, json={"status": "done", "exit_code": 1, "stdout": "out", "stderr": "err"}
        ),
    ]

    result = await _sandbox().run("pytest -q", timeout=1800)

    assert result == ExecResult(exit_code=1, stdout="out", stderr="err")
    assert exec_route.called
    assert status_route.call_count == 2


async def test_run_with_on_stdout_dispatches_to_stream_exec_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    async def _fake_stream_exec_async(
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

    monkeypatch.setattr("quome._async.sandbox.stream_exec_async", _fake_stream_exec_async)

    received: list[str] = []
    result = await _sandbox().run("tail -f log", on_stdout=received.append)

    assert result == ExecResult(exit_code=None, stdout="chunk", stderr="")
    assert received == ["chunk"]
    assert calls == [(ORG_ID, SANDBOX_ID, "tail -f log", "/workspace", None, 300)]


# --- files round trip --------------------------------------------------


@respx.mock
async def test_files_write_then_read_round_trip() -> None:
    write_route = respx.put(f"{SANDBOX_URL}/files").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"{SANDBOX_URL}/files/download").mock(
        return_value=httpx.Response(200, content=b"hello world")
    )

    sbx = _sandbox()
    await sbx.files.write("report.txt", "hello world")
    content = await sbx.files.read("report.txt")

    assert content == b"hello world"
    sent = write_route.calls.last.request
    assert sent.url.params["path"] == "report.txt"
    assert b"hello world" in sent.content


@respx.mock
async def test_files_list_returns_entry_names() -> None:
    respx.get(f"{SANDBOX_URL}/files").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "a.py", "type": "file"},
                {"name": "sub", "type": "directory"},
            ],
        )
    )

    entries = await _sandbox().files.list()

    assert entries == ["a.py", "sub"]


@respx.mock
async def test_files_delete_hits_files_endpoint_with_path_param() -> None:
    route = respx.delete(f"{SANDBOX_URL}/files").mock(return_value=httpx.Response(204))

    await _sandbox().files.delete("old.txt")

    assert route.called
    assert route.calls.last.request.url.params["path"] == "old.txt"


# --- preview_url -------------------------------------------------------


def test_preview_url_requires_public_kwarg() -> None:
    # Missing-required-keyword-only-argument binding happens at call time,
    # before the coroutine is even created — this raises without awaiting.
    with pytest.raises(TypeError):
        _sandbox().preview_url(8000)  # type: ignore[call-arg]


async def test_preview_url_public_false_raises_value_error() -> None:
    with pytest.raises(ValueError, match="internet-reachable"):
        await _sandbox().preview_url(8000, public=False)


@respx.mock
async def test_preview_url_public_true_posts_ports_and_returns_server_url() -> None:
    route = respx.post(f"{SANDBOX_URL}/ports").mock(
        return_value=httpx.Response(
            200, json={"port": 8000, "url": "https://sbx-abc123-8000.acme.quome.dev"}
        )
    )

    url = await _sandbox().preview_url(8000, public=True)

    assert url == "https://sbx-abc123-8000.acme.quome.dev"
    body = route.calls.last.request.content
    assert b'"port": 8000' in body or b'"port":8000' in body
