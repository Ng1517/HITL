# Langflow Human-in-the-Loop "Wait for Approval"

A working prototype of an asynchronous HITL approval gate for Langflow: an AI
Agent's output is emailed to a reviewer, who approves or rejects it from a
secure web page, and the decision is durably recorded and can resume the
workflow — even if the reviewer takes hours, or Langflow restarts in the
meantime.

---

## 1. Architectural analysis (read this before the code)

### Can a Langflow custom component pause execution and resume later on an inbound HTTP request?

**No — not in the general "wait indefinitely, then wake up" sense.** This was
checked against Langflow's actual execution model rather than assumed:

- A Langflow flow run is a single, bounded Python call stack that starts when
  the flow is triggered (Playground message, `/api/v1/run/...`, a scheduled
  job, etc.) and ends when the outputs are produced. A custom component's
  `build`/output methods execute synchronously (or as an `async` coroutine)
  *within* that call. There is no built-in checkpointing layer that
  serializes an in-progress flow's Python state to disk and resumes it from
  an arbitrary suspension point later, the way e.g. LangGraph's
  `interrupt()`/`Command` + `CheckpointSaver` does, or the way Temporal/
  Mistral's durable-workflow signals do.
- Langflow's own maintainers have confirmed native, first-class HITL
  (pausable multi-agent execution with a resume mechanism) is **on the
  roadmap but not shipped** as a core primitive you can rely on in a custom
  component today (see `langflow-ai/langflow` discussion #5221 and issue
  #6867). Treat any "Wait for Approval" node you see in a Langflow UI
  screenshot as a *pattern to reproduce*, not evidence of a built-in
  suspend/resume primitive you can hook into from arbitrary Python.
- A custom component also cannot receive an inbound HTTP request itself —
  it isn't a server, it has no route, and it isn't kept alive between calls.
  Something else has to receive the reviewer's click.

**Conclusion:** the "waiting" cannot live inside the Langflow process at all.
It has to live in an ordinary, always-on web service with its own database.
That's `approval_service/`.

### The two honest ways to use this within that constraint

| | Fire-and-forget (recommended) | Poll (blocking) |
|---|---|---|
| How it works | Component creates the request, sends the email, returns immediately via the `Pending` output. A **second, continuation** Langflow flow is invoked later by the Approval Service via Langflow's Run API when a decision arrives. | Component loops calling the Approval Service's status endpoint, inside the *same* flow run, until a decision arrives or `max_wait_seconds` elapses. |
| Survives Langflow restart? | Yes — state lives in the Approval Service's DB. The continuation call is a fresh flow run. | No — if Langflow restarts mid-poll, that run is gone. The approval request itself is unaffected (still pending in the DB) but nothing is left running to notice the eventual decision. |
| Appropriate for | Minutes to days. This is the actual "asynchronous" pattern the brief asks for. | Seconds to a few minutes, invoked by something willing to hold a connection open that long (a script, a queue worker) — not the interactive Playground. |
| Requires | A second "continuation" flow in Langflow, and `LANGFLOW_CONTINUATION_ENABLED=true` on the service. | Nothing extra, but ties up a flow run (and whatever called it) the whole time. |

This is why the component's **Approved** and **Rejected** outputs cannot
simply "eventually fire" on their own — nothing in Langflow can reach into a
finished flow run and push a value into it later. In fire-and-forget mode,
those two outputs are correctly suppressed (via `self.stop(...)`) every
time, and only `Pending / Info` fires, carrying `status="pending"`. The real
Approved/Rejected branching happens **in the continuation flow**, whose
input is the JSON payload the Approval Service posts to it.

### The "Continuation Flow" pattern in practice

1. Build your main flow: `Input → AI Agent → Wait for Approval`. Nothing is
   connected downstream of `Approved`/`Rejected` in this flow — there's no
   point, they'll never fire here in fire-and-forget mode. Optionally wire
   `Pending / Info` to a "log/notify the requester" step.
2. Build a **second flow**, e.g. `Approval Decision Received`, whose input
   is the JSON the Approval Service sends:
   `{"status": "...", "request_id": "...", "response": "...", "reviewer_comment": "...", "approver_inputs": {...}, "workflow_metadata": {...}}`.
   Inside it, use a Conditional Router (or an `If status == "approved"`
   check) to branch into your real "do the approved thing" / "do the
   rejected thing" logic — the same nodes you originally imagined living
   after the HITL component's Approved/Rejected outputs.
3. Get that second flow's ID and its Run API URL
   (`http(s)://<host>/api/v1/run/<flow-id>`), put it in
   `LANGFLOW_API_URL`, and set `LANGFLOW_CONTINUATION_ENABLED=true` on the
   Approval Service.
4. `workflow_metadata` (currently `flow_id`/`session_id` of the *original*
   run) is threaded through so the continuation flow can correlate back to
   the original conversation/session if your downstream logic needs that.
   Extend it with whatever else your flow needs to resume meaningfully
   (e.g. a ticket ID, a user ID) — it's stored as opaque JSON.

If you don't need automatic continuation (e.g. you just want a dashboard of
pending/approved/rejected items, or your own external orchestrator polls
`GET /internal/approval-requests/{id}`), leave
`LANGFLOW_CONTINUATION_ENABLED=false`. The service still does everything
else (email, page, persistence, security).

---

## 2. The Langflow component: inputs and outputs

`custom_components/human_in_the_loop/wait_for_approval.py`, class
`WaitForApprovalComponent`.

### Inputs

| Name | Type | Required | Purpose |
|---|---|---|---|
| `ai_response` | Message / Data / str | yes | The upstream Agent's output. Coerced to plain text internally (`Message.text`, `Data.get_text()`, or `str(...)`). |
| `reviewer_email` | string | yes | Where the approval email is sent. |
| `email_subject` | string | no (default provided) | Email subject line. |
| `approval_message` | multiline string | no (default provided) | Instructional text shown above the AI response, in both the email and the review page. |
| `approver_inputs` | table | no | Zero or more extra fields the reviewer fills in. Each row: `field_name`, `label`, `type` (`text`/`number`/`boolean`/`select`), `options` (comma-separated, for `select`), `description`, `required`. Leave empty for a plain Approve/Reject with no form — this is fully optional, per the requirement that the basic flow must work without it. |
| `approval_service_url` | string | yes | Base URL of the FastAPI service, e.g. `https://approvals.yourdomain.com`. |
| `internal_api_key` | secret string | yes | Shared secret matching the service's `INTERNAL_API_KEY`. Never sent to the reviewer. |
| `ttl_minutes` | int (advanced) | no (default 1440) | Approval link lifetime. |
| `wait_mode` | dropdown | no | `Fire-and-forget (recommended)` or `Poll (blocking, short waits only)`. See §1. |
| `max_wait_seconds`, `poll_interval_seconds` | int (advanced) | Poll mode only | Bound and cadence of the blocking poll loop. |
| `fail_on_email_error` | bool (advanced) | no | If true, a failed email send turns into an `error` status instead of a silent `pending`. |

### Outputs

| Name | Fires when | Payload (a `Data` object) |
|---|---|---|
| `Approved` | Poll mode and the reviewer approved before `max_wait_seconds`. Never fires in fire-and-forget mode (see §1). | `{"status": "approved", "request_id", "response", "reviewer_email", "reviewer_comment", "approver_inputs", "reviewed_at"}` |
| `Rejected` | Poll mode and the reviewer rejected in time. Never fires in fire-and-forget mode. | Same shape, `status="rejected"`. |
| `Pending / Info` | Fire-and-forget mode (always); Poll mode on timeout, expiry, or error. | `{"status": "pending"\|"timeout"\|"expired"\|"error", "request_id", "response", "email_sent", ...}` |

The three output methods (`run_approved`, `run_rejected`, `run_pending`) all
call a shared `_resolve()` that does the actual HTTP work exactly once per
flow run (cached on `self._resolved_cache`) and then use Langflow's
`self.stop("<output_name>")` to suppress the outputs that shouldn't fire —
the same mechanism Langflow's own conditional-routing components use to
make only one branch of a graph execute.

---

## 3. Approval lifecycle

```
PENDING --(reviewer approves in time, token valid)--> APPROVED
PENDING --(reviewer rejects in time, token valid)---> REJECTED
PENDING --(now > expires_at)------------------------> EXPIRED
```

- `PENDING → APPROVED` and `PENDING → REJECTED` are the only writable
  transitions. There is no `APPROVED → REJECTED` or vice versa, and no way
  to re-open an `EXPIRED` request — a new one must be created by re-running
  the flow.
- Every transition is a single conditional SQL `UPDATE ... WHERE status =
  'pending'` (see `crud.record_decision`). If two requests race (double
  click, two people with the same link — see §7), only the first commit
  changes any rows; the loser is told the request was already resolved.
  This makes "prevent reuse", "prevent approving an already-rejected
  request", and "prevent double-approval" all the same guarantee: **the
  status column can only ever leave PENDING once.**
- Expiry is enforced twice: lazily (any read of a stale PENDING row flips it
  to EXPIRED on the spot, see `crud._expire_if_needed`) and proactively (a
  background `apscheduler` job sweeps every 5 minutes). This means an
  expired link is correctly rejected even if the sweeper hasn't run yet,
  and the DB is kept tidy even if nobody happens to click an expired link.

---

## 4. Persistence

`approval_service/database.py` defines `ApprovalRequest` via SQLAlchemy,
matching the table sketched in the brief (`id`, `token_hash`, `ai_response`,
`reviewer_email`, `status`, `created_at`, `expires_at`, `reviewed_at`,
`reviewer_comment`, plus `approver_input_schema`/`approver_inputs` as JSON
and a `workflow_metadata` JSON blob for the continuation pattern).

- **Dev**: SQLite (`DATABASE_URL=sqlite:///./approvals.db`), zero setup.
- **Prod**: change `DATABASE_URL` to e.g.
  `postgresql+psycopg2://user:pass@host:5432/approvals` and `pip install
  psycopg2-binary` — nothing else in the codebase changes, because all
  access goes through SQLAlchemy's `Session`, not raw SQLite calls.
- The raw approval token is **never stored**. Only
  `sha256(token).hexdigest()` (`token_hash`) is persisted; lookups hash the
  incoming token and compare hashes. A database read alone cannot produce a
  working approval link.

---

## 5. Email sending

`approval_service/email_service.py` defines an `EmailProvider` interface
with three implementations out of the box: `SMTPEmailProvider` (default,
configured entirely via env vars — host/port/username/password/TLS-or-SSL/
sender), `ConsoleEmailProvider` (prints the email instead of sending it —
useful for local development so you don't need real SMTP credentials to
test the rest of the system), and `ResendEmailProvider` as a worked example
of swapping in an API-based provider. Adding SendGrid/Mailgun/etc. means
adding one more subclass with a `send()` method; nothing else in `main.py`
changes. Credentials are read only from `config.settings`, i.e. from
environment variables — never hard-coded, never logged.

The HTML email (`templates/email_approval.html`) shows the approval
message, the AI response (HTML-escaped via `bleach`, see §7), Approve/Reject
buttons that link to the review page, and the request ID + expiry. Both
buttons point at the *same* URL (`/approval/{token}`) — the actual
Approve-vs-Reject choice is made on that page, which is what allows
approver-input fields (if configured) to be collected before the decision
is finalized, and prevents a mail client's "link prefetching" from silently
triggering an approval (see §7).

---

## 6. HTTP endpoints

Internal (require header `X-Internal-Api-Key`, called by the Langflow
component, never exposed to reviewers):

- `POST /internal/approval-requests` — create a request, send the email.
  Body: `{ai_response, reviewer_email, email_subject, approval_message,
  ttl_minutes, approver_input_schema, workflow_metadata}`. Returns
  `{request_id, status, expires_at, email_sent, review_url}`.
- `GET /internal/approval-requests/{request_id}` — current status, used by
  Poll mode and by any dashboard/orchestrator you want to build.

Public (opened by the reviewer; no header auth, protected instead by the
unguessable token + rate limiting + single-use semantics):

- `GET /approval/{token}` — review page: AI response + any configured
  approver-input fields + Approve/Reject buttons.
- `POST /approval/{token}/decide` — form submission from that page; records
  the decision (or returns an "already processed"/"expired"/"invalid" page).
- `GET /approval/{token}/approve` and `GET /approval/{token}/reject` —
  one-click shortcuts, but **only honored when the request has zero
  configured approver-input fields**; otherwise they redirect the reviewer
  into the same form so required fields can't be bypassed by a bare GET.
- `GET /healthz` — liveness check.

---

## 7. Security

- **Tokens**: `secrets.token_urlsafe(32)` — 256 bits of CSPRNG entropy.
  Generated once, put in the email link, never logged, never stored raw
  (only its SHA-256 hash).
- **Expiration**: configurable per-request `ttl_minutes`, enforced lazily
  and by a background sweeper (§3).
- **Single use / no reuse / no cross-transitions**: enforced by the atomic
  conditional UPDATE described in §3 — this simultaneously satisfies
  "prevent reuse after processed", "prevent approving an already-rejected
  request", and "prevent double approval".
- **Token validation**: every public endpoint hashes the incoming token and
  looks it up by hash; an unknown hash renders a generic "Invalid Link"
  page (no information about whether *some* token exists is leaked).
- **No credentials in URLs**: the only thing in a URL is the opaque token;
  SMTP/API credentials live exclusively in the Approval Service's
  environment and are never returned in any HTTP response.
- **HTML sanitization**: the AI response is untrusted input. Before it is
  ever interpolated into the email or the review page,
  `security.sanitize_for_html()` runs it through `bleach.clean()` with an
  empty allow-list by default — this HTML-escapes and strips all markup,
  neutralizing `<script>`, event handlers, etc., so a prompt-injected or
  adversarial AI response can't execute in the reviewer's browser. (A
  `allow_restricted_html=True` mode exists if you later want a small safe
  subset of formatting tags — off by default.)
- **Abuse protection on callback endpoints**: `security.SlidingWindowRateLimiter`
  applies a simple per-IP, per-minute cap to the public `/approval/...`
  routes. It's in-process and fine for a single-worker deployment; if you
  run multiple workers/instances, swap it for a shared store (Redis) — the
  interface (`allow(key) -> bool`) is deliberately tiny to make that a
  local change.
- **Internal endpoints**: require `X-Internal-Api-Key`, compared with
  `hmac.compare_digest` (constant-time) to avoid timing side-channels.
- **HTTPS**: `PUBLIC_BASE_URL` must be `https://` in production — plain
  HTTP would leak the approval token (which is bearer-token-equivalent) in
  transit and in browser history/referrers. This prototype runs over HTTP
  locally for convenience only; put it behind a reverse proxy (nginx,
  Caddy, your cloud LB) terminating TLS before exposing it beyond
  localhost.

---

## 8. Environment variables

See `approval_service/.env.example` for the authoritative, copy-pasteable
list. Summary:

| Variable | Required | Notes |
|---|---|---|
| `PUBLIC_BASE_URL` | yes | Must be `https://` in production. |
| `DATABASE_URL` | yes | `sqlite:///./approvals.db` for dev. |
| `INTERNAL_API_KEY` | yes | Long random string; shared with the Langflow component's `internal_api_key` input. |
| `DEFAULT_TTL_MINUTES` | no | Default approval link lifetime. |
| `LOG_LEVEL` | no | Python logging level. |
| `RATE_LIMIT_PER_MINUTE` | no | Public endpoint rate limit. |
| `EMAIL_PROVIDER` | no | `smtp` \| `resend` \| `console`. |
| `SMTP_HOST`,`SMTP_PORT`,`SMTP_USERNAME`,`SMTP_PASSWORD`,`SMTP_USE_TLS`,`SMTP_USE_SSL`,`SENDER_EMAIL`,`SENDER_NAME` | if `EMAIL_PROVIDER=smtp` | Standard SMTP settings. |
| `RESEND_API_KEY` | if `EMAIL_PROVIDER=resend` | |
| `LANGFLOW_CONTINUATION_ENABLED` | no | See §1's Continuation Flow pattern. |
| `LANGFLOW_API_URL`,`LANGFLOW_API_KEY` | if continuation enabled | Run-API URL of your continuation flow. |

---

## 9. Required packages

`approval_service/requirements.txt`:
`fastapi`, `uvicorn[standard]`, `sqlalchemy`, `pydantic`, `pydantic-settings`,
`jinja2`, `python-multipart`, `bleach`, `apscheduler`, `httpx`, and
optionally `resend`.

The Langflow custom component only needs `httpx`, which is already a
Langflow dependency — no extra install inside Langflow itself.

---

## 10. Installation

### 10.1 Approval Service

```bash
cd approval_service
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: set INTERNAL_API_KEY to a long random string, and either
#   EMAIL_PROVIDER=console  (for a first local smoke test, no SMTP needed)
# or real SMTP_* / SENDER_EMAIL values.
uvicorn main:app --reload --port 8000
```

Visit `http://localhost:8000/healthz` — should return `{"status": "ok", ...}`.

### 10.2 Custom component in Langflow

```bash
# Point Langflow at a folder of custom components. LANGFLOW_COMPONENTS_PATH
# can list multiple paths; category subfolders become the sidebar section.
export LANGFLOW_COMPONENTS_PATH=/absolute/path/to/langflow-hitl/custom_components
langflow run
```

Langflow scans that path on startup; a new sidebar category named after the
folder (`human_in_the_loop`) should appear containing **Wait for Approval**.
If you're running Langflow Desktop or a hosted instance without shell env
var access, use the in-UI "Custom Component" code editor instead: create a
new custom component and paste the contents of `wait_for_approval.py` in.

### 10.3 Wire it up

1. Drag **Agent** (or any component producing a Message/Data/text output)
   onto the canvas, configure it normally.
2. Drag **Wait for Approval** onto the canvas. Connect the Agent's output
   to `AI Response`.
3. Fill in `Reviewer Email`, `Approval Service URL` (`http://localhost:8000`
   for local testing), and `Internal API Key` (must match `.env`'s
   `INTERNAL_API_KEY`). Leave `Approver Inputs` empty for the simplest test.
4. Leave `Wait Mode` on **Fire-and-forget** for the realistic async test
   (see §12), or switch to **Poll** if you want to see Approved/Rejected
   fire in the same run for a quick synchronous demo.
5. Connect `Approved` and `Rejected` outputs to whatever should happen next
   (e.g. two separate Chat Output nodes) — these only fire in Poll mode, or
   inside your continuation flow if you build one per §1.

---

## 11. Component reference: how each requirement was met

| Brief requirement | Where |
|---|---|
| Unique approval request ID | `crud.create_approval_request` → `uuid.uuid4()` as `request_id` |
| Email with response + Approve/Reject buttons | `templates/email_approval.html` |
| Secure links, no sensitive info in URL | token-only URLs, §7 |
| Separate Approved/Rejected outputs | component `outputs` list, §2 |
| Original response preserved | `response` field on every output payload |
| Approver-configurable input fields (text/number/boolean/select) | `TableInput` on the component + `approver_input_schema` rendered by `page_review.html` |
| Works with zero approver fields | `approver_inputs=[]` default; one-click `/approve` `/reject` shortcuts activate automatically in that case |
| Professional result pages, not raw JSON | `templates/page_result.html` |

---

## 12. End-to-end test procedure

1. Start the Approval Service with `EMAIL_PROVIDER=console` (no real SMTP
   needed) as in §10.1. Watch its terminal — that's where the "email" will
   be printed.
2. In Langflow, build `Chat Input → Agent → Wait for Approval`, configured
   as in §10.3, `Wait Mode = Fire-and-forget`. Run the flow (Playground or
   `POST /api/v1/run/<flow-id>`).
3. Confirm the flow returns quickly with a `Pending / Info` output like
   `{"status": "pending", "request_id": "...", "email_sent": true, ...}`,
   and that the Approval Service's console printed an email containing a
   `http://localhost:8000/approval/<token>` link.
4. Open that link in a browser. Confirm you see the AI response and
   Approve/Reject buttons (and, if you added an `Approver Inputs` row named
   `reviewer_comment`, a text box for it).
5. Click **Approve**. Confirm you land on the "Approval Successful" page,
   and that `GET /internal/approval-requests/<request_id>` (with header
   `X-Internal-Api-Key: <your key>`) now returns `"status": "approved"`.
6. Reload the same approval link. Confirm you now get the "Already
   Processed" page, not a second decision prompt — this exercises the
   reuse/double-decision protection in §3/§7.
7. Repeat steps 2–5 but let the link sit unused past `ttl_minutes` (or set
   `ttl_minutes=1` for a fast test); confirm the link now shows "Link
   Expired" and the DB row's status is `expired`.
8. (Optional, continuation flow) Build a second flow per §1's "Continuation
   Flow" pattern, set `LANGFLOW_CONTINUATION_ENABLED=true` and
   `LANGFLOW_API_URL` to its Run API URL, restart the Approval Service, and
   repeat step 5 — confirm the continuation flow executes and its
   `If status == approved` branch is taken.
9. (Optional, Poll mode) Switch `Wait Mode` to **Poll**, set
   `max_wait_seconds=120`, run the flow, and approve the link from another
   browser tab within two minutes — confirm the `Approved` output fires in
   the *same* flow run this time, with `Rejected`/`Pending` suppressed.

---

## 13. Behavior under the specific scenarios asked for

- **Reviewer never responds**: request stays `PENDING` until `expires_at`,
  then the background sweeper (or the next read) flips it to `EXPIRED`. No
  workflow continuation is triggered. In Poll mode the flow run ends with
  `status="timeout"` once `max_wait_seconds` is reached, independent of
  the Approval Service's own (typically much longer) `ttl_minutes`.
- **The approval expires**: any further click on that link renders "Link
  Expired"; `record_decision` cannot transition an `EXPIRED` row (only
  `PENDING → APPROVED/REJECTED` is legal), so no decision can be recorded
  after the fact.
- **Reviewer clicks Approve twice**: the first click's `UPDATE ... WHERE
  status='pending'` wins and commits; the row is no longer `PENDING` when
  the second click's identical UPDATE runs, so it affects 0 rows, and
  `crud.record_decision` raises `AlreadyResolvedError`, rendered as
  "Already Processed". No double side effects.
- **Reviewer clicks Approve after Reject** (or vice versa): same mechanism
  — the row is already non-`PENDING`, so the second transition is refused
  and "Already Processed" is shown. The original decision (`REJECTED`)
  stands.
- **Langflow process restarts**: irrelevant to already-created approval
  requests (they live entirely in the Approval Service's DB). In
  fire-and-forget mode, nothing was waiting in Langflow anyway. In Poll
  mode, the in-progress flow run (and its poll loop) is lost, but the
  underlying approval request is untouched and can still be decided;
  nothing will *act* on that decision automatically unless you also have
  continuation enabled or re-poll it another way.
- **The approval API (this service) restarts**: in-memory rate-limiter
  state and the APScheduler job reset, but all approval requests persist
  in the database (SQLite file or Postgres) and are unaffected. Any email
  already sent still has a working link, since validity is determined by
  the DB row, not by service uptime.
- **Email fails to send**: `main.create_approval_request` catches the
  exception, logs it, and still returns `email_sent: false` with the
  request successfully created (unless `fail_on_email_error=True` on the
  component, in which case the component surfaces this as an `error`
  status instead of silently reporting `pending`). The request is fully
  functional if you obtain the link another way (e.g. a dashboard you
  build against `GET /internal/approval-requests/{id}`, or a manual resend
  feature you add).
- **Two reviewers receive the same approval email** (e.g. it was forwarded,
  or `Notify Emails` conceptually lists more than one): whichever one
  clicks Approve/Reject first wins, by the same atomic-UPDATE mechanism as
  double-clicking; the second reviewer sees "Already Processed" with no
  ambiguity about which decision stands.
- **Reviewer submits a comment**: if an `approver_inputs` row named
  `reviewer_comment` is configured, its value is stored both in the
  dedicated `reviewer_comment` column and inside the generic
  `approver_inputs` JSON blob, and is included in the payload sent to the
  continuation flow / returned by the status endpoint.
- **AI response contains HTML or malicious-looking content**: never
  rendered as live HTML. `security.sanitize_for_html()` (via `bleach`)
  strips/escapes all markup before the text is interpolated into either
  the email or the review page, so `<script>` tags, `onerror=` handlers,
  etc. are neutralized and shown as inert text.

---

## 14. Production hardening notes (beyond this prototype)

- Swap `DATABASE_URL` to Postgres and run behind a process manager
  (systemd/Docker) with more than one Uvicorn worker; move the rate
  limiter to Redis if you do, since the in-process one is per-worker.
- Put a reverse proxy in front terminating TLS (`PUBLIC_BASE_URL=https://...`).
- Add a retry queue around `webhook.notify_continuation` if you need
  guaranteed delivery of the continuation call (currently best-effort: the
  decision is always durably saved even if this call fails).
- Consider signing/short-TTL-ing the continuation webhook call itself if
  your Langflow instance is reachable from more than trusted infrastructure.
