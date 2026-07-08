from __future__ import annotations

import httpx
import pytest
import respx

from quome import NotFoundError, Template, Transport
from quome._org import OrgResolver, resolve_org
from quome.templates import list_templates, resolve_template

BASE_URL = "https://api.quome.studio"

ORG_ID = "11111111-1111-1111-1111-111111111111"
TEMPLATE_ID_A = "22222222-2222-2222-2222-222222222222"
TEMPLATE_ID_B = "33333333-3333-3333-3333-333333333333"


# --- resolve_org ---------------------------------------------------------


def test_resolve_org_uses_env_override_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUOME_ORG_ID", ORG_ID)
    transport = Transport(api_key="sk_test_key")

    with respx.mock:
        # No routes registered at all — any HTTP call raises inside respx.mock.
        org_id = resolve_org(transport)

    assert org_id == ORG_ID


@respx.mock
def test_resolve_org_calls_self_endpoint_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QUOME_ORG_ID", raising=False)
    route = respx.get(f"{BASE_URL}/api/v1/api-keys/self").mock(
        return_value=httpx.Response(
            200,
            json={
                "org_id": ORG_ID,
                "service_account_id": "44444444-4444-4444-4444-444444444444",
                "scopes": ["*"],
            },
        )
    )
    transport = Transport(api_key="sk_test_key")

    org_id = resolve_org(transport)

    assert org_id == ORG_ID
    assert route.called


@respx.mock
def test_org_resolver_memoizes_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUOME_ORG_ID", raising=False)
    route = respx.get(f"{BASE_URL}/api/v1/api-keys/self").mock(
        return_value=httpx.Response(
            200,
            json={"org_id": ORG_ID, "service_account_id": None, "scopes": ["*"]},
        )
    )
    transport = Transport(api_key="sk_test_key")
    resolver = OrgResolver(transport)

    first = resolver.resolve()
    second = resolver.resolve()

    assert first == ORG_ID
    assert second == ORG_ID
    assert route.call_count == 1


def test_org_resolver_instances_do_not_share_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUOME_ORG_ID", ORG_ID)
    transport_a = Transport(api_key="sk_a")
    transport_b = Transport(api_key="sk_b")

    resolver_a = OrgResolver(transport_a)
    resolver_b = OrgResolver(transport_b)

    assert resolver_a.resolve() == ORG_ID
    assert resolver_b.resolve() == ORG_ID
    assert resolver_a is not resolver_b


# --- list_templates --------------------------------------------------------


@respx.mock
def test_list_templates_parses_enveloped_response() -> None:
    respx.get(f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandbox-templates").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": TEMPLATE_ID_A,
                        "name": "python-3.11",
                        "image": "gcr.io/foo/python:3.11",
                        "default_resources": {"cpu": "1", "memory": "512Mi"},
                    }
                ],
                "meta": {"total": 1},
            },
        )
    )
    transport = Transport(api_key="sk_test_key")

    templates = list_templates(transport, ORG_ID)

    assert templates == [
        Template(
            id=TEMPLATE_ID_A,
            name="python-3.11",
            image="gcr.io/foo/python:3.11",
            default_resources={"cpu": "1", "memory": "512Mi"},
        )
    ]


@respx.mock
def test_list_templates_parses_bare_list_response() -> None:
    respx.get(f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandbox-templates").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": TEMPLATE_ID_B,
                    "name": "node-20",
                    "image": "gcr.io/foo/node:20",
                    "default_resources": {"cpu": "2", "memory": "1Gi"},
                }
            ],
        )
    )
    transport = Transport(api_key="sk_test_key")

    templates = list_templates(transport, ORG_ID)

    assert templates == [
        Template(
            id=TEMPLATE_ID_B,
            name="node-20",
            image="gcr.io/foo/node:20",
            default_resources={"cpu": "2", "memory": "1Gi"},
        )
    ]


@respx.mock
def test_list_templates_maps_defensively_with_missing_fields() -> None:
    respx.get(f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandbox-templates").mock(
        return_value=httpx.Response(200, json=[{"id": TEMPLATE_ID_A}])
    )
    transport = Transport(api_key="sk_test_key")

    templates = list_templates(transport, ORG_ID)

    assert templates == [
        Template(id=TEMPLATE_ID_A, name="", image="", default_resources={}),
    ]


# --- resolve_template --------------------------------------------------------


def test_resolve_template_uuid_passthrough_without_http_call() -> None:
    transport = Transport(api_key="sk_test_key")

    with respx.mock:
        resolved = resolve_template(transport, ORG_ID, TEMPLATE_ID_A)

    assert resolved == TEMPLATE_ID_A


@respx.mock
def test_resolve_template_case_insensitive_name_match() -> None:
    respx.get(f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandbox-templates").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": TEMPLATE_ID_A,
                        "name": "Python-3.11",
                        "image": "gcr.io/foo/python:3.11",
                        "default_resources": {},
                    }
                ]
            },
        )
    )
    transport = Transport(api_key="sk_test_key")

    resolved = resolve_template(transport, ORG_ID, "python-3.11")

    assert resolved == TEMPLATE_ID_A


@respx.mock
def test_resolve_template_unknown_name_raises_not_found_with_available_names() -> None:
    respx.get(f"{BASE_URL}/api/v1/orgs/{ORG_ID}/sandbox-templates").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": TEMPLATE_ID_A,
                        "name": "python-3.11",
                        "image": "gcr.io/foo/python:3.11",
                        "default_resources": {},
                    },
                    {
                        "id": TEMPLATE_ID_B,
                        "name": "node-20",
                        "image": "gcr.io/foo/node:20",
                        "default_resources": {},
                    },
                ]
            },
        )
    )
    transport = Transport(api_key="sk_test_key")

    with pytest.raises(NotFoundError) as excinfo:
        resolve_template(transport, ORG_ID, "does-not-exist")

    message = str(excinfo.value)
    assert "python-3.11" in message
    assert "node-20" in message
