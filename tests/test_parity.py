"""Anti-drift guard: the sync and async surfaces must expose the same public
method/property names.

Comparison is name-only, not signature-only or type-only — e.g.
``Quome.org_id`` is a synchronous ``@property`` while ``AsyncQuome.org_id``
is a coroutine method (``await client.org_id()``); that's an expected,
structural consequence of "await this instead of reading it synchronously"
and isn't something this test polices. What it does police is someone adding
a whole method to one side (sync or async) and forgetting the other — that's
exactly the kind of drift that's easy to miss in review but breaks parity
silently.
"""

from __future__ import annotations

from quome._async.client import AsyncQuome
from quome._async.files import AsyncSandboxFiles
from quome._async.sandbox import AsyncSandbox
from quome.client import Quome
from quome.files import SandboxFiles
from quome.sandbox import Sandbox

#: Members intentionally present on only one side, with the reason.
#: {class pair label: {"sync": {names}, "async": {names}}}
#: Empty on purpose — every public member below has a same-named counterpart
#: across sync vs async today. Add an entry here (with a comment) if a
#: future method is genuinely meant to be one-sided.
_ALLOWED_ASYMMETRY: dict[str, dict[str, set[str]]] = {}


def _public_members(cls: type) -> set[str]:
    """Public (non-underscore-prefixed) names defined directly on ``cls``.

    Deliberately uses ``vars(cls)`` (the class's own ``__dict__``), not
    ``dir(cls)`` — this counts methods/properties/classmethods declared on
    the class itself, not instance attributes set in ``__init__`` (e.g.
    ``self.files``, which both ``Sandbox`` and ``AsyncSandbox`` set
    identically and so is a non-issue for drift) and not inherited ``object``
    members (``__repr__`` is redefined on both anyway, but starts with ``_``
    so it's excluded regardless).
    """
    return {name for name in vars(cls) if not name.startswith("_")}


def _assert_parity(label: str, sync_cls: type, async_cls: type) -> None:
    sync_members = _public_members(sync_cls)
    async_members = _public_members(async_cls)

    allowed = _ALLOWED_ASYMMETRY.get(label, {})
    sync_only = (sync_members - async_members) - allowed.get("sync", set())
    async_only = (async_members - sync_members) - allowed.get("async", set())

    messages = []
    if sync_only:
        messages.append(f"only on {sync_cls.__name__}: {sorted(sync_only)}")
    if async_only:
        messages.append(f"only on {async_cls.__name__}: {sorted(async_only)}")

    assert not messages, f"{label} parity drift — " + "; ".join(messages)


def test_quome_async_quome_public_method_parity() -> None:
    _assert_parity("Quome/AsyncQuome", Quome, AsyncQuome)


def test_sandbox_async_sandbox_public_method_parity() -> None:
    _assert_parity("Sandbox/AsyncSandbox", Sandbox, AsyncSandbox)


def test_files_async_files_public_method_parity() -> None:
    _assert_parity("SandboxFiles/AsyncSandboxFiles", SandboxFiles, AsyncSandboxFiles)
