"""Async mirror of the top-level ``quome`` package.

Everything here is a coroutine-based twin of a sync counterpart living one
directory up — ``AsyncQuome`` mirrors ``quome.client.Quome``, ``AsyncSandbox``
mirrors ``quome.sandbox.Sandbox``, ``AsyncSandboxFiles`` mirrors
``quome.files.SandboxFiles``. Request-building and response-parsing logic is
shared with the sync modules wherever practical (imported directly, or
factored into a small ``*_async`` sibling in the shared module); the intent
is that the only difference between a sync method and its async twin is the
``await``. ``tests/test_parity.py`` enforces that the two surfaces expose the
same public method names, so they can't silently drift apart.

Import ``AsyncQuome`` / ``AsyncSandbox`` / ``AsyncSandboxFiles`` from the
top-level ``quome`` package rather than this subpackage directly.
"""

from __future__ import annotations
