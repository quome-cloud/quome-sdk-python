"""Shared test fixtures for the quome SDK test suite.

Two things every test needs:

- A clean environment: QUOME_API_KEY / QUOME_BASE_URL / QUOME_ORG_ID must not
  leak between tests, and must not leak in from whatever shell ran pytest.
- No real sleeping: the transport's retry backoff calls time.sleep /
  asyncio.sleep, and the sandbox status / async-job pollers in _poll.py call
  time.sleep between attempts. Tests patch all of these to no-ops so the
  suite stays fast and deterministic regardless of the jitter/backoff values
  chosen.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUOME_API_KEY", raising=False)
    monkeypatch.delenv("QUOME_BASE_URL", raising=False)
    monkeypatch.delenv("QUOME_ORG_ID", raising=False)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("quome._transport.time.sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr("quome._poll.time.sleep", lambda *_a, **_kw: None)

    async def _fake_asleep(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr("quome._transport.asyncio.sleep", _fake_asleep)
    yield
