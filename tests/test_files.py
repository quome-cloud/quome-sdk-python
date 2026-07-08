from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from quome import Quome, Sandbox

BASE_URL = "https://api.quome.studio"
ORG_ID = "11111111-1111-1111-1111-111111111111"
SANDBOX_ID = "22222222-2222-2222-2222-222222222222"

SANDBOX_URL = f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandboxes/{SANDBOX_ID}"

# A genuinely binary, non-UTF-8 payload (PNG-ish magic bytes + 0xff 0xfe).
BINARY_PAYLOAD = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0xFF, 0xFE])


@pytest.fixture(autouse=True)
def _org_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUOME_ORG_ID", ORG_ID)


def _sandbox() -> Sandbox:
    client = Quome(api_key="sk_test_key")
    data: dict[str, Any] = {
        "id": SANDBOX_ID,
        "status": "running",
        "proxy_subdomain": "sbx-abc",
        "exposed_ports": [],
        "expires_at": None,
        "created_at": None,
        "started_at": None,
    }
    return Sandbox(client, data)


# --- write ------------------------------------------------------------


@respx.mock
def test_write_sends_multipart_with_file_field_and_path_query_param() -> None:
    write_route = respx.put(f"{SANDBOX_URL}/files").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    _sandbox().files.write("report.txt", "hello world")

    sent = write_route.calls.last.request
    assert sent.url.params["path"] == "report.txt"
    content_type = sent.headers["content-type"]
    assert content_type.startswith("multipart/form-data")
    assert b'name="file"' in sent.content
    assert b"hello world" in sent.content


@respx.mock
def test_write_str_content_is_utf8_encoded() -> None:
    write_route = respx.put(f"{SANDBOX_URL}/files").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    _sandbox().files.write("notes.txt", "héllo")

    sent = write_route.calls.last.request
    assert "héllo".encode() in sent.content


@respx.mock
def test_write_binary_payload_round_trips_without_decoding() -> None:
    write_route = respx.put(f"{SANDBOX_URL}/files").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    _sandbox().files.write("image.png", BINARY_PAYLOAD)

    sent = write_route.calls.last.request
    # The exact raw bytes must appear verbatim in the multipart body — no
    # UTF-8 decode/re-encode round trip that would corrupt non-UTF-8 bytes.
    assert BINARY_PAYLOAD in sent.content


@respx.mock
def test_write_then_read_round_trip() -> None:
    respx.put(f"{SANDBOX_URL}/files").mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.get(f"{SANDBOX_URL}/files/download").mock(
        return_value=httpx.Response(200, content=b"hello world")
    )

    sbx = _sandbox()
    sbx.files.write("report.txt", "hello world")
    content = sbx.files.read("report.txt")

    assert content == b"hello world"


# --- read --------------------------------------------------------------


@respx.mock
def test_read_returns_response_content_bytes_verbatim() -> None:
    respx.get(f"{SANDBOX_URL}/files/download").mock(
        return_value=httpx.Response(200, content=BINARY_PAYLOAD)
    )

    result = _sandbox().files.read("image.png")

    assert result == BINARY_PAYLOAD


@respx.mock
def test_read_sends_path_as_query_param_on_download_endpoint() -> None:
    route = respx.get(f"{SANDBOX_URL}/files/download").mock(
        return_value=httpx.Response(200, content=b"data")
    )

    result = _sandbox().files.read("a/b.txt")

    assert result == b"data"
    sent = route.calls.last.request
    assert sent.url.params["path"] == "a/b.txt"


# --- list --------------------------------------------------------------


@respx.mock
def test_list_returns_names_from_bare_list_of_entries() -> None:
    respx.get(f"{SANDBOX_URL}/files").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "a.py", "type": "file"},
                {"name": "sub", "type": "directory"},
            ],
        )
    )

    entries = _sandbox().files.list()

    assert entries == ["a.py", "sub"]


@respx.mock
def test_list_drops_entries_missing_name() -> None:
    respx.get(f"{SANDBOX_URL}/files").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"name": "a.py", "type": "file"},
                {"type": "file"},
                {"name": "", "type": "file"},
                {"name": "b.py", "type": "file"},
            ],
        )
    )

    entries = _sandbox().files.list()

    assert entries == ["a.py", "b.py"]


@respx.mock
def test_list_uses_default_path_and_custom_path() -> None:
    route = respx.get(f"{SANDBOX_URL}/files").mock(return_value=httpx.Response(200, json=[]))

    _sandbox().files.list()
    assert route.calls.last.request.url.params["path"] == "/workspace"

    _sandbox().files.list("/workspace/sub")
    assert route.calls.last.request.url.params["path"] == "/workspace/sub"


# --- delete --------------------------------------------------------------


@respx.mock
def test_delete_hits_files_endpoint_with_path_param() -> None:
    route = respx.delete(f"{SANDBOX_URL}/files").mock(return_value=httpx.Response(204))

    _sandbox().files.delete("old.txt")

    assert route.called
    assert route.calls.last.request.url.params["path"] == "old.txt"
