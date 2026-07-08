"""The Sandbox resource: lifecycle, exec dispatch, files, preview_url.

All routes live under ``/api/v1/orgs/{org_id}/sandboxes``. See the design
doc's "Sandbox surface (v1)" and "Exec paths" sections for the endpoint
table and the exec-mode rationale.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ._models import ExecResult
from ._poll import TERMINAL_FAIL_STATES, poll_job, wait_for_status
from ._stream import stream_exec
from .client import Quome, default_client
from .files import SandboxFiles
from .templates import resolve_template

_SYNC_EXEC_TIMEOUT_CEILING = 240.0


def _as_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object in the response, got {type(payload).__name__}")
    return payload


class Sandbox:
    """A Quome sandbox: an isolated, ephemeral compute environment.

    Construct via :meth:`create` or :meth:`get` rather than the constructor
    directly.
    """

    def __init__(self, client: Quome, data: dict[str, Any]) -> None:
        self._client = client
        self._data = data
        self.files = SandboxFiles(self)

    # -- fields -----------------------------------------------------------

    @property
    def id(self) -> str:
        return str(self._data["id"])

    @property
    def status(self) -> str:
        return str(self._data["status"])

    @property
    def proxy_subdomain(self) -> str | None:
        value = self._data.get("proxy_subdomain")
        return str(value) if value is not None else None

    @property
    def exposed_ports(self) -> list[int]:
        return list(self._data.get("exposed_ports") or [])

    @property
    def expires_at(self) -> str | None:
        value = self._data.get("expires_at")
        return str(value) if value is not None else None

    @property
    def created_at(self) -> str | None:
        value = self._data.get("created_at")
        return str(value) if value is not None else None

    @property
    def started_at(self) -> str | None:
        value = self._data.get("started_at")
        return str(value) if value is not None else None

    @property
    def org_id(self) -> str:
        return self._client.org_id

    def _base_path(self) -> str:
        return f"/api/v1/orgs/{self.org_id}/sandboxes/{self.id}"

    def _get(self, suffix: str = "") -> dict[str, Any]:
        response = self._client.transport.request("GET", f"{self._base_path()}{suffix}")
        return _as_dict(response.json())

    def _post(self, suffix: str = "", json: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.transport.request("POST", f"{self._base_path()}{suffix}", json=json)
        return _as_dict(response.json())

    # -- create / get / list ------------------------------------------------

    @classmethod
    def create(
        cls,
        template: str,
        name: str | None = None,
        resources: dict[str, Any] | None = None,
        idle_timeout: int | None = None,
        wait: bool = True,
        timeout: float = 300,
        client: Quome | None = None,
    ) -> Sandbox:
        """Create a sandbox from ``template`` (a template name or id).

        ``template`` is resolved via :func:`~quome.templates.resolve_template`
        — pass either a template id (a UUID, used as-is) or a human-friendly
        template name (resolved with a ``GET /sandbox-templates`` lookup).

        When ``wait`` is ``True`` (the default), blocks until the sandbox
        reaches ``running``. A terminal failure state (``failed``, ``error``,
        ``deleted``) raises :class:`~quome._exceptions.SandboxProvisioningError`
        immediately, without waiting out the rest of ``timeout``; genuinely
        not reaching ``running`` in time raises ``TimeoutError``.
        """
        client = client or default_client()
        org_id = client.org_id
        template_id = resolve_template(client.transport, org_id, template)

        body: dict[str, Any] = {"template_id": template_id}
        if name is not None:
            body["name"] = name
        if resources is not None:
            body["resources"] = resources
        if idle_timeout is not None:
            body["idle_timeout"] = idle_timeout

        response = client.transport.request("POST", f"/api/v1/orgs/{org_id}/sandboxes", json=body)
        sandbox = cls(client, _as_dict(response.json()))

        if wait:
            sandbox._wait_until_running(timeout)

        return sandbox

    def _wait_until_running(self, timeout: float) -> None:
        self._data = wait_for_status(
            self._get, want="running", terminal_fail=TERMINAL_FAIL_STATES, timeout=timeout
        )

    @classmethod
    def get(cls, sandbox_id: str, client: Quome | None = None) -> Sandbox:
        """Fetch a single sandbox by id."""
        client = client or default_client()
        org_id = client.org_id
        response = client.transport.request("GET", f"/api/v1/orgs/{org_id}/sandboxes/{sandbox_id}")
        return cls(client, _as_dict(response.json()))

    @classmethod
    def list(cls, client: Quome | None = None) -> list[Sandbox]:
        """List all sandboxes in the org."""
        client = client or default_client()
        org_id = client.org_id
        response = client.transport.request("GET", f"/api/v1/orgs/{org_id}/sandboxes")
        payload: Any = response.json()
        items = payload.get("data", []) if isinstance(payload, dict) else payload
        return [cls(client, _as_dict(item)) for item in items]

    # -- lifecycle ----------------------------------------------------------

    def refresh(self) -> Sandbox:
        """Re-fetch this sandbox's state from the API and update in place."""
        self._data = self._get()
        return self

    def stop(self) -> Sandbox:
        self._data = self._post("/stop")
        return self

    def resume(self) -> Sandbox:
        self._data = self._post("/resume")
        return self

    def delete(self) -> None:
        self._client.transport.request("DELETE", self._base_path())

    def extend(self) -> Sandbox:
        self._data = self._post("/extend")
        return self

    # -- exec -----------------------------------------------------------

    def run(
        self,
        cmd: str,
        timeout: float = 300,
        working_dir: str = "/workspace",
        env: dict[str, str] | None = None,
        on_stdout: Callable[[str], None] | None = None,
    ) -> ExecResult:
        """Run ``cmd`` in the sandbox and return its result.

        Dispatch is honest about mode (see the SDK design doc's "Exec
        paths"):

        - ``on_stdout`` given: incremental output over the exec WebSocket
          (requires the ``quome[ws]`` extra — see :mod:`quome._stream`).
          The streaming protocol carries no exit code and no separate
          stderr channel, so the returned ``ExecResult`` always has
          ``exit_code=None`` and ``stderr=""`` on this path; use it only
          when you don't need the exit code.
        - no ``on_stdout``, ``timeout <= 240``: a single synchronous
          ``POST /exec`` round trip.
        - no ``on_stdout``, ``timeout > 240``: submits an async exec job and
          polls it to completion. Output is delivered only once the job is
          done — there is no partial output in this mode.
        """
        if on_stdout is not None:
            return stream_exec(
                self._client.transport,
                self.org_id,
                self.id,
                cmd,
                working_dir,
                env,
                on_stdout,
                timeout,
            )

        if timeout <= _SYNC_EXEC_TIMEOUT_CEILING:
            return self._run_sync(cmd, timeout, working_dir, env)

        return self._run_async_job(cmd, timeout, working_dir, env)

    def _run_sync(
        self, command: str, timeout: float, working_dir: str, env: dict[str, str] | None
    ) -> ExecResult:
        payload = self._post(
            "/exec",
            json={
                "command": command,
                "timeout": timeout,
                "working_dir": working_dir,
                "env": env or {},
                "mode": "sync",
            },
        )
        return _exec_result_from_payload(payload)

    def _run_async_job(
        self, command: str, timeout: float, working_dir: str, env: dict[str, str] | None
    ) -> ExecResult:
        submitted = self._post(
            "/exec",
            json={
                "command": command,
                "timeout": timeout,
                "working_dir": working_dir,
                "env": env or {},
                "mode": "async",
            },
        )
        job_id = submitted["job_id"]

        payload = poll_job(lambda: self._get(f"/exec/{job_id}"), timeout=timeout)
        return _exec_result_from_payload(payload)

    # -- preview ----------------------------------------------------------

    def preview_url(self, port: int, *, public: bool) -> str:
        """Expose ``port`` and return its public preview URL.

        ``public`` is required and keyword-only — calling without it raises
        ``TypeError``. The returned URL is **publicly internet-reachable**:
        anyone with the link can hit the exposed port with no additional
        authentication (an unguessable subdomain is not access control).
        Passing ``public=False`` raises ``ValueError`` rather than silently
        doing nothing — there is no non-public preview mode in v1.
        """
        if not public:
            raise ValueError("preview_url requires public=True; the URL is internet-reachable")

        payload = self._post("/ports", json={"port": port})
        return str(payload["url"])

    def __repr__(self) -> str:
        return f"Sandbox(id={self.id!r}, status={self.status!r})"


def _exec_result_from_payload(payload: dict[str, Any]) -> ExecResult:
    return ExecResult(
        exit_code=int(payload["exit_code"]),
        stdout=str(payload.get("stdout", "")),
        stderr=str(payload.get("stderr", "")),
    )
