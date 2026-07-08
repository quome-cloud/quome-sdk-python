# quome

The official Python SDK for [Quome](https://quome.studio) sandboxes — isolated,
ephemeral compute environments you can spin up, run commands in, exchange
files with, and expose a preview URL from, all from a few lines of Python.

```bash
pip install quome
```

Streaming exec (`run(..., on_stdout=...)`) needs the `ws` extra:

```bash
pip install quome[ws]
```

Requires Python 3.10+.

## Quickstart

```python
import quome

sbx = quome.Sandbox.create(template="Code Write")  # blocks until running
result = sbx.run("pytest -q")
print(result.exit_code, result.stdout)

sbx.files.write("report.txt", "all green\n")
print(sbx.files.read("report.txt"))

url = sbx.preview_url(8000, public=True)  # see "Preview URLs" below
print(url)

sbx.stop()
```

### Async

Every sync call has an `async`/`await` mirror under `AsyncQuome` /
`AsyncSandbox`, with identical method names and semantics:

```python
import asyncio
import quome

async def main() -> None:
    sbx = await quome.AsyncSandbox.create(template="Code Write")
    result = await sbx.run("pytest -q")
    print(result.exit_code, result.stdout)
    await sbx.stop()

asyncio.run(main())
```

Note that `org_id` is an async method on `AsyncQuome` (`await client.org_id()`),
not a property — resolving it makes a network call the first time it's used.

## Authentication

The SDK reads your API key from the `QUOME_API_KEY` environment variable (or
pass `api_key=` explicitly to `quome.Quome(...)` / `quome.AsyncQuome(...)`):

```bash
export QUOME_API_KEY=qk_...
```

**Use a service-account key with sandbox grants, not your org-owner key.**
Create one under **Settings → Service Accounts → API keys** and grant it only
the sandbox permissions it needs, rather than reusing the full-owner key from
the general **Settings → API Keys** page. A service-account key is scoped to
exactly the resources you grant it (least privilege); an org-owner key can do
anything in the org, so a leaked owner key is a much bigger blast radius than
a leaked, narrowly-scoped one.

The org id for the key is resolved automatically (`GET /api/v1/api-keys/self`,
cached on the client) — you normally don't need to set it. `QUOME_ORG_ID` is
available as an override for multi-org service accounts.

`base_url` defaults to `https://api.quome.studio`; override with
`QUOME_BASE_URL` or `base_url=` for self-hosted / dev environments.

## Secrets

Never put secret values inline in a command string passed to `run()` — exec
commands are audit-logged verbatim, so anything in the command text ends up
in the audit trail in plaintext. Pass secrets through `env=` instead (or
inject a secret binding into the sandbox ahead of time):

```python
# Wrong — the API key literal lands in the audit log
sbx.run(f"curl -H 'Authorization: Bearer {api_key}' https://example.com")

# Correct — env values are not part of the logged command text
sbx.run("curl -H \"Authorization: Bearer $API_KEY\" https://example.com", env={"API_KEY": api_key})
```

## Preview URLs

```python
url = sbx.preview_url(8000, public=True)
```

`public=True` is required (keyword-only) — there is no way to call
`preview_url` without explicitly opting in. The returned URL is **publicly
internet-reachable**: anyone who has the link can hit the exposed port with
no further authentication. An unguessable subdomain is not access control —
don't put anything behind it that requires real auth unless the app itself
enforces it.

## Streaming exec

```python
def on_chunk(text: str) -> None:
    print(text, end="")

result = sbx.run("some-long-build.sh", on_stdout=on_chunk)
```

Streaming requires the `quome[ws]` extra (`pip install quome[ws]`) — without
it, `run(..., on_stdout=...)` raises a clear `QuomeError` telling you to
install it. The streaming protocol carries incremental stdout only: it has no
exit-code channel and no separate stderr, so a streamed `ExecResult` always
has `exit_code=None` and `stderr=""`. If you need the exit code, call
`run()` without `on_stdout` (a normal synchronous or job-polled exec, which
does return `exit_code`).

## Files

```python
sbx.files.write("app.py", source_code)          # str or bytes; creates or overwrites
data = sbx.files.read("app.py")                  # raw bytes
names = sbx.files.list("/workspace")             # entry names in a directory
sbx.files.delete("app.py")
```

## Errors

All SDK exceptions derive from `quome.QuomeError`. HTTP errors from the API
map to typed subclasses of `quome.QuomeAPIError`:

| Exception | When |
|---|---|
| `AuthenticationError` | 401/403, or no API key configured |
| `NotFoundError` | 404 |
| `QuotaExceededError` | 429 (carries `retry_after` when the server sends one) |
| `SandboxNotRunningError` | 409 against a sandbox that isn't running |
| `SandboxProvisioningError` | a sandbox lands in a terminal failure state while `Sandbox.create(wait=True)` is waiting for `running` |
| `QuomeAPIError` | any other non-2xx response (fallback) |

None of these ever carry the API key — only HTTP status codes and
server-provided detail strings.

## API stability

The SDK wraps a specific, documented subset of the Quome REST API (sandbox
lifecycle, exec, files, preview URLs, templates, key introspection). That
subset is the SDK's **stable public contract** — changes to it are additive
only, and breaking changes ship as an SDK major version bump. Everything else
under `/api/v1` is internal to the Quome web app and may change without
notice; don't call undocumented endpoints directly and expect stability. See
`docs/developer-guide/python-sdk.md` in the main repo for the full endpoint
table.

## License

Apache-2.0
