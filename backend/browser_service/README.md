# Browser execution service

The service is deliberately independent of Flask, SQLAlchemy and the durable task
queue. All Playwright objects belong to its one asyncio event loop. The worker owns
leases, retry decisions, source coalescing and database publication.

## Launch and configuration

Run `python scripts/run_browser.py` with these environment settings:

| Setting | Default / purpose |
| --- | --- |
| `WATCHER_BROWSER_TOKEN` | Required, at least 32 characters. Same secret in web/worker. |
| `WATCHER_BROWSER_HOST` | `127.0.0.1`; use `0.0.0.0` only inside a private container network. |
| `WATCHER_BROWSER_PORT` | `8765` |
| `WATCHER_BROWSER_URL` | Web/worker URL, e.g. `http://browser:8765`. |
| `WATCHER_ORIGIN_BROKER_URL` | Private main-app admission endpoint, e.g. `http://web:5000/internal/browser-origin`; launchers configure it. |
| `WATCHER_BROWSER_CONCURRENCY` | `1`; resource ceiling, not extra retries. |
| `WATCHER_BROWSER_MAX_SESSIONS` | `8`; origin/source contexts, idle expiry 30 minutes. |
| `WATCHER_BROWSER_MAX_EXECUTIONS` | `128`; completed responses expire after five minutes, HTML capped at 64 MiB total. |
| `WATCHER_BROWSER_EGRESS_PROXY` | Optional deployment-managed forward proxy for public-only egress. |
| `WATCHER_BROWSER_CHANNEL` | Optional `msedge` or `chrome` for desktop installations. Default uses the pinned bundled Chromium. |
| `WATCHER_PUBLIC_ORIGIN` | Exact public origin (`https://watcher.example`) required for remote manual verification. |

Install the pinned Playwright package and its matching browser with
`python -m playwright install chromium`. Respect `PLAYWRIGHT_BROWSERS_PATH` when
provided; no personal path is embedded in code. Linux runs as a non-root user
with Chromium sandbox enabled. Do not expose the private API/VNC ports publicly.

## Internal HTTP API

All ordinary endpoints require `X-Watcher-Token`. JSON requests are limited to
64 KiB; IDs are opaque strings. HTTP 202 is an accepted execution, not success.

- `GET /health`: service boot identity (does not prove a browser binary is installed).
- `POST /v1/fetch`: serialized `FetchRequest` with a stable `request_id`.
- `GET /v1/fetch/{request_id}`: `queued`, `running`, `done` or `failed` envelope.
  `done.result` contains final document URL/status/headers, rendered HTML and timing.
  `failed.error` contains a typed code, plain message and retryable flag.
- `POST /v1/manual`: FetchRequest plus `id` and secret `generation`; opens a fresh
  headed source session. A second active manual session returns HTTP 409.
- `GET /v1/manual/{id}`: `X-Session-Generation` required, returns boot identity and status.
- `POST /v1/manual/{id}/{verify|cancel|ticket}`: JSON `generation` required.

An identical request ID and payload returns its existing execution; conflicting
payloads return HTTP 409. In-memory results disappearing after expiry or service
restart return HTTP 404 so the durable worker can safely resubmit. Content validation
belongs to `backend.scraper.acquisition.classify_result`, including manual completion.

When all execution slots are occupied, admission returns `browser_busy`; resource
waiting stays in the durable queue without consuming failure attempts. Main-page
redirects and resources reaching another host request a database-backed permit
through the private admission endpoint. Same-page resources share their host permit;
the browser does not maintain a second business queue or connect to the database.

Native Playwright timeouts bound navigation and response waits. A supervisor watchdog
exits the isolated service with code 75 if an operation exceeds its budget by 15 seconds;
the process supervisor restarts it and the worker recovers using its existing lease.
There is no cancellation of a Playwright API coroutine midway through an operation.

## Remote manual verification

Linux dependencies: `Xvfb`, TigerVNC's `x0vncserver`, and the noVNC browser client.
Every headed session has a separate disposable display. The VNC listener binds
loopback only and exists only while its verification is active. Completing or
cancelling verification, or reaching its 10-minute deadline, closes active sockets,
revokes tickets and terminates VNC. Successful verification retains the same browser
context for subsequent fetching; no cookies are copied into a different browser.

The Flask admin page is `/admin/access-verification`. The reverse proxy must:

1. Proxy `/browser-access/{id}/websocket` to
   `/v1/manual/{id}/websocket` on this service, supporting WebSocket Upgrade.
2. Authenticate every handshake with Flask's
   `GET /api/admin/browser-access/auth`, forwarding the user's Cookie header.
3. Preserve Origin and require TLS for public deployments.
4. Disable access logging for the WebSocket location: its query includes a one-use,
   60-second ticket. The runtime checks the origin and independently consumes that ticket.
5. Serve local noVNC assets at `/browser-client/` (e.g. `/usr/share/novnc/`).

The web UI requests tickets through a CSRF-protected administrator POST. Service tokens
and session generations never appear in ordinary API responses, templates or exports.
Windows uses an application-owned visible Chromium window and needs no remote display.

## Validation

`python -m unittest discover -s tests -p "test_browser_runtime*.py"` includes isolated
runtime ownership/security tests and Flask permission/CSRF tests. Real rendering tests
run against localhost-only fixtures when bundled Chromium exists or
`WATCHER_TEST_BROWSER_CHANNEL=msedge` is supplied. A test-only injected address validator
allows that fixture origin; production URL validation is never relaxed.
