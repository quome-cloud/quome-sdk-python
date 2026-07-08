"""Sandbox template listing and name/id resolution.

``resolve_template`` is the piece ``Sandbox.create(template=...)`` (task 7)
will call so callers can pass either a template id (a UUID, returned
unchanged with no HTTP call) or a human-friendly template name (resolved via
a case-insensitive lookup against :func:`list_templates`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ._exceptions import NotFoundError
from ._transport import AsyncTransport, Transport


@dataclass(frozen=True)
class Template:
    """A sandbox template available to an org."""

    id: str
    name: str
    image: str
    default_resources: dict[str, str]


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _parse_templates(payload: Any) -> list[Template]:
    # The list endpoint returns either a bare list or a paginated envelope
    # ({"data": [...], "meta": {...}}) depending on the route — handle both.
    items = payload.get("data", []) if isinstance(payload, dict) else payload
    templates = []
    for item in items:
        templates.append(
            Template(
                id=item.get("id", ""),
                name=item.get("name", ""),
                image=item.get("image", ""),
                default_resources=item.get("default_resources") or {},
            )
        )
    return templates


def list_templates(transport: Transport, org_id: str) -> list[Template]:
    """Return the sandbox templates available to ``org_id``."""
    response = transport.request("GET", f"/api/v1/orgs/{org_id}/sandbox-templates")
    return _parse_templates(response.json())


async def list_templates_async(transport: AsyncTransport, org_id: str) -> list[Template]:
    """Async mirror of :func:`list_templates` over an :class:`AsyncTransport`."""
    response = await transport.request("GET", f"/api/v1/orgs/{org_id}/sandbox-templates")
    return _parse_templates(response.json())


def _no_match_error(name_or_id: str, templates: list[Template]) -> NotFoundError:
    available = ", ".join(sorted(t.name for t in templates)) or "(none)"
    return NotFoundError(
        status_code=None,
        detail=f"no sandbox template named {name_or_id!r}. Available templates: {available}",
    )


def resolve_template(transport: Transport, org_id: str, name_or_id: str) -> str:
    """Resolve a template name or id to a template id.

    A value that parses as a UUID is returned unchanged (no HTTP call) —
    it's assumed to already be a template id. Otherwise this lists the org's
    templates and matches ``name_or_id`` case-insensitively against template
    names, raising :class:`NotFoundError` (with the available names in the
    message) when nothing matches.
    """
    if _is_uuid(name_or_id):
        return name_or_id

    templates = list_templates(transport, org_id)
    lowered = name_or_id.lower()
    for template in templates:
        if template.name.lower() == lowered:
            return template.id

    raise _no_match_error(name_or_id, templates)


async def resolve_template_async(transport: AsyncTransport, org_id: str, name_or_id: str) -> str:
    """Async mirror of :func:`resolve_template` over an :class:`AsyncTransport`."""
    if _is_uuid(name_or_id):
        return name_or_id

    templates = await list_templates_async(transport, org_id)
    lowered = name_or_id.lower()
    for template in templates:
        if template.name.lower() == lowered:
            return template.id

    raise _no_match_error(name_or_id, templates)
