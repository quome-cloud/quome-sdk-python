from __future__ import annotations

import httpx
import pytest
import respx

from quome import (
    AsyncTransport,
    AuthenticationError,
    NotFoundError,
    QuomeAPIError,
    QuotaExceededError,
    SandboxNotRunningError,
    Transport,
    __version__,
)

BASE_URL = "https://api.quome.studio"

# (status_code, json_body, expected_exception_type)
ERROR_CASES = [
    pytest.param(401, {"detail": "bad credentials"}, AuthenticationError, id="401-authentication"),
    pytest.param(403, {"detail": "forbidden"}, AuthenticationError, id="403-authentication"),
    pytest.param(404, {"detail": "sandbox not found"}, NotFoundError, id="404-not-found"),
    pytest.param(429, {"detail": "rate limited"}, QuotaExceededError, id="429-quota-exceeded"),
    pytest.param(
        409,
        {"detail": "sandbox sbx_1 is not_running"},
        SandboxNotRunningError,
        id="409-sandbox-not-running",
    ),
    pytest.param(
        409,
        {"detail": "sandbox sbx_1 is stopped"},
        SandboxNotRunningError,
        id="409-sandbox-stopped",
    ),
    pytest.param(
        409, {"detail": "conflict: duplicate name"}, QuomeAPIError, id="409-generic-conflict"
    ),
    pytest.param(500, {"detail": "internal error"}, QuomeAPIError, id="500-server-error"),
    pytest.param(400, {"detail": "bad request"}, QuomeAPIError, id="400-bad-request"),
]


# --- headers -----------------------------------------------------------


@respx.mock
def test_sync_request_sends_auth_and_user_agent_headers() -> None:
    route = respx.get(f"{BASE_URL}/v1/ping").mock(return_value=httpx.Response(200, json={}))

    transport = Transport(api_key="sk_test_key")
    transport.request("GET", "/v1/ping")

    sent = route.calls.last.request
    assert sent.headers["X-API-Key"] == "sk_test_key"
    assert sent.headers["User-Agent"] == f"quome-python/{__version__}"


@respx.mock
async def test_async_request_sends_auth_and_user_agent_headers() -> None:
    route = respx.get(f"{BASE_URL}/v1/ping").mock(return_value=httpx.Response(200, json={}))

    transport = AsyncTransport(api_key="sk_test_key")
    await transport.request("GET", "/v1/ping")

    sent = route.calls.last.request
    assert sent.headers["X-API-Key"] == "sk_test_key"
    assert sent.headers["User-Agent"] == f"quome-python/{__version__}"


# --- error mapping (table-tested) --------------------------------------


@respx.mock
@pytest.mark.parametrize("status_code,body,expected_exc", ERROR_CASES)
def test_sync_error_mapping(
    status_code: int, body: dict[str, str], expected_exc: type[Exception]
) -> None:
    respx.post(f"{BASE_URL}/v1/things").mock(return_value=httpx.Response(status_code, json=body))
    transport = Transport(api_key="sk_test_key")

    with pytest.raises(expected_exc) as excinfo:
        transport.request("POST", "/v1/things")

    assert isinstance(excinfo.value, QuomeAPIError)
    assert excinfo.value.status_code == status_code
    assert body["detail"] in str(excinfo.value)


@respx.mock
@pytest.mark.parametrize("status_code,body,expected_exc", ERROR_CASES)
async def test_async_error_mapping(
    status_code: int, body: dict[str, str], expected_exc: type[Exception]
) -> None:
    respx.post(f"{BASE_URL}/v1/things").mock(return_value=httpx.Response(status_code, json=body))
    transport = AsyncTransport(api_key="sk_test_key")

    with pytest.raises(expected_exc):
        await transport.request("POST", "/v1/things")


@respx.mock
def test_quota_exceeded_carries_retry_after() -> None:
    respx.post(f"{BASE_URL}/v1/things").mock(
        return_value=httpx.Response(
            429, json={"detail": "rate limited"}, headers={"Retry-After": "12"}
        )
    )
    transport = Transport(api_key="sk_test_key")

    with pytest.raises(QuotaExceededError) as excinfo:
        transport.request("POST", "/v1/things")

    assert excinfo.value.retry_after == 12.0


@respx.mock
def test_2xx_returns_normally() -> None:
    respx.get(f"{BASE_URL}/v1/ok").mock(return_value=httpx.Response(200, json={"ok": True}))
    transport = Transport(api_key="sk_test_key")

    response = transport.request("GET", "/v1/ok")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


# --- key hygiene ---------------------------------------------------------


SECRET = "sk_live_super_secret_value_12345"  # noqa: S105


def test_key_never_in_sync_transport_repr() -> None:
    transport = Transport(api_key=SECRET)
    assert SECRET not in repr(transport)
    assert SECRET not in str(transport)


def test_key_never_in_async_transport_repr() -> None:
    transport = AsyncTransport(api_key=SECRET)
    assert SECRET not in repr(transport)
    assert SECRET not in str(transport)


@respx.mock
def test_key_never_in_exception_str_on_auth_failure() -> None:
    respx.post(f"{BASE_URL}/v1/things").mock(
        return_value=httpx.Response(401, json={"detail": "bad credentials"})
    )
    transport = Transport(api_key=SECRET)

    with pytest.raises(AuthenticationError) as excinfo:
        transport.request("POST", "/v1/things")

    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)


def test_key_never_in_missing_key_exception_str() -> None:
    exc = AuthenticationError("QUOME_API_KEY not set")
    assert "QUOME_API_KEY not set" in str(exc)
    assert "QUOME_API_KEY not set" in repr(exc)


# --- env fallback ----------------------------------------------------------


@respx.mock
def test_env_fallback_for_key_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUOME_API_KEY", "sk_from_env")
    monkeypatch.setenv("QUOME_BASE_URL", "https://custom.example.com")
    route = respx.get("https://custom.example.com/v1/ping").mock(
        return_value=httpx.Response(200, json={})
    )

    transport = Transport()
    transport.request("GET", "/v1/ping")

    sent = route.calls.last.request
    assert sent.headers["X-API-Key"] == "sk_from_env"


# --- missing key is lazy ----------------------------------------------------


def test_missing_key_does_not_raise_at_construction() -> None:
    # Should not raise even though no key is set anywhere.
    Transport()
    AsyncTransport()


@respx.mock
def test_missing_key_raises_at_first_request() -> None:
    respx.get(f"{BASE_URL}/v1/ping").mock(return_value=httpx.Response(200, json={}))
    transport = Transport()

    with pytest.raises(AuthenticationError) as excinfo:
        transport.request("GET", "/v1/ping")

    assert "QUOME_API_KEY not set" in str(excinfo.value)


@respx.mock
async def test_missing_key_raises_at_first_request_async() -> None:
    respx.get(f"{BASE_URL}/v1/ping").mock(return_value=httpx.Response(200, json={}))
    transport = AsyncTransport()

    with pytest.raises(AuthenticationError) as excinfo:
        await transport.request("GET", "/v1/ping")

    assert "QUOME_API_KEY not set" in str(excinfo.value)


# --- GET retries -------------------------------------------------------


@respx.mock
def test_get_retries_on_503_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/v1/flaky")
    route.side_effect = [
        httpx.Response(503, json={"detail": "unavailable"}),
        httpx.Response(200, json={"ok": True}),
    ]
    transport = Transport(api_key="sk_test_key")

    response = transport.request("GET", "/v1/flaky")

    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
def test_get_retries_exhaust_and_raise() -> None:
    route = respx.get(f"{BASE_URL}/v1/always-down").mock(
        return_value=httpx.Response(503, json={"detail": "unavailable"})
    )
    transport = Transport(api_key="sk_test_key")

    with pytest.raises(QuomeAPIError):
        transport.request("GET", "/v1/always-down")

    assert route.call_count == 3


@respx.mock
def test_get_retries_on_transport_error_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/v1/flaky-conn")
    route.side_effect = [
        httpx.ConnectError("boom"),
        httpx.Response(200, json={"ok": True}),
    ]
    transport = Transport(api_key="sk_test_key")

    response = transport.request("GET", "/v1/flaky-conn")

    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
def test_post_does_not_retry_on_503() -> None:
    route = respx.post(f"{BASE_URL}/v1/things").mock(
        return_value=httpx.Response(503, json={"detail": "unavailable"})
    )
    transport = Transport(api_key="sk_test_key")

    with pytest.raises(QuomeAPIError):
        transport.request("POST", "/v1/things")

    assert route.call_count == 1


@respx.mock
async def test_async_get_retries_on_503_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/v1/flaky")
    route.side_effect = [
        httpx.Response(503, json={"detail": "unavailable"}),
        httpx.Response(200, json={"ok": True}),
    ]
    transport = AsyncTransport(api_key="sk_test_key")

    response = await transport.request("GET", "/v1/flaky")

    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_async_post_does_not_retry_on_503() -> None:
    route = respx.post(f"{BASE_URL}/v1/things").mock(
        return_value=httpx.Response(503, json={"detail": "unavailable"})
    )
    transport = AsyncTransport(api_key="sk_test_key")

    with pytest.raises(QuomeAPIError):
        await transport.request("POST", "/v1/things")

    assert route.call_count == 1
