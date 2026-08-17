# Langflow Human-in-the-Loop "Wait for Approval"

An asynchronous HITL approval gate for Langflow: an AI Agent's output is
emailed to a reviewer, who approves or rejects it from a secure page, and
the decision is durably recorded — even if the reviewer takes hours, or the
service restarts in the meantime.

## Architecture

**Why this can't live inside Langflow alone.** A Langflow custom component
runs inside a single, bounded flow execution. It cannot stay alive for
hours/days waiting for a reviewer to click a link, and it cannot receive an
inbound HTTP request itself — there's no built-in mechanism to pause a flow
run and resume it later from an arbitrary external event. So the actual
"waiting" has to live outside Langflow, in an always-on service with its own
database. That's what this project is.

```
Langflow flow                Approval Service (FastAPI + Postgres)          Reviewer
──────────────                ─────────────────────────────────              ────────
AI Agent
   │
   ▼
Wait for Approval  ──POST──▶  create request, store in DB,
component                     email Approve/Reject links
   │                                   │
   ▼                                   ▼
Pending / Info                  email delivered ───────────────────────▶  opens link
 output (flow                                                              │
 run ends here)                                                            ▼
                                                                     confirm page
                                                                    (single button
                                                                     for that
                                                                     decision)
                                                                            │
                               ◀──POST /decide───────────────────────────  clicks
                               atomically flips PENDING → APPROVED/REJECTED
                               (only the first click wins; replay-safe)
                                       │
                     (optional) POST to a Langflow
                     "continuation flow" Run API URL
                     with the decision, to resume
                     your real Approved/Rejected logic
```

**Two wait modes on the component**, because a single component genuinely
can't do both:

- **Fire-and-forget (recommended):** creates the request, emails it, returns
  immediately via `Pending / Info`. A second Langflow flow gets triggered
  later, when the decision actually arrives, via the Approval Service
  calling Langflow's Run API. This survives restarts and works for
  waits of minutes to days.
- **Poll (blocking):** the component loops calling the service's status
  endpoint inside the *same* flow run, up to a max wait. Simpler, but ties
  up the run the whole time and doesn't survive a Langflow restart. Only
  sensible for short waits.

**Security model, briefly:** approval tokens are 256-bit random values;
only their SHA-256 hash is ever stored, so a DB leak alone can't produce a
working link. Every decision is a single atomic `UPDATE ... WHERE
status='pending'`, which is what makes double-approval, approve-after-reject,
and replayed links all impossible — whichever request hits first wins, and
every later attempt sees "already processed." Links expire on a TTL, checked
both lazily and by a background sweep. The AI response is HTML-escaped
before it's ever rendered, so it can't inject markup into the email or the
review page. The email's Approve/Reject links are read-only `GET`s that
render a one-button confirm page — the actual decision only happens on that
button's `POST` — specifically so that automated link-prescanning by mail
clients/security gateways can't silently trigger a decision before a human
reads the email.

## Deployment (Render + Postgres)

1. **Database**: create a Render Postgres instance. Copy its internal
   connection string.
2. **Approval Service**: create a Render Web Service from this repo's
   `approval_service/` folder.
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Environment variables:
     - `DATABASE_URL` — the Postgres connection string, with the driver
       prefix changed to `postgresql+psycopg2://...` (SQLAlchemy needs this;
       Render's default string usually starts with `postgresql://`, which
       still works but add `psycopg2-binary` to `requirements.txt` either
       way).
     - `PUBLIC_BASE_URL` — your Render URL, e.g.
       `https://your-app.onrender.com`. Must match exactly or the emailed
       links will be wrong.
     - `INTERNAL_API_KEY` — a long random string; the Langflow component
       must be configured with the same value.
     - `EMAIL_PROVIDER=resend` (or `brevo`) — **not** `smtp`. Render's free
       tier blocks all outbound SMTP ports (25/465/587), so SMTP-based
       sending will time out regardless of provider. Resend and Brevo both
       send over HTTPS, which isn't affected.
     - `RESEND_API_KEY` / `SENDER_EMAIL` (or the Brevo equivalents) — see
       your email provider's dashboard.
     - `LANGFLOW_CONTINUATION_ENABLED` / `LANGFLOW_API_URL` /
       `LANGFLOW_API_KEY` — only if you're using the fire-and-forget +
       continuation-flow pattern; otherwise leave disabled.
3. **Langflow component**: set `Approval Service URL` to your Render URL,
   and `Internal API Key` to the same `INTERNAL_API_KEY` value.
4. **Free tier caveats to know about**: Render free web services spin down
   when idle and cold-start on the next request (a reviewer's first click
   after idle time may take 10–30s). If you're on Resend without a verified
   domain, it will only deliver to the email address you signed up with —
   verify a domain when you're ready to email real reviewers.

## End-to-end test

1. Run the flow in Langflow; confirm it returns a `pending` status quickly
   and an email goes out.
2. Open the email, click Approve — confirm you land on a one-button confirm
   page, not an instant decision.
3. Click "Confirm Approval" — confirm you see the success page.
4. Reload the same link — confirm you now see "Already Processed."
5. Check `GET /internal/approval-requests/{request_id}` (with your
   `X-Internal-Api-Key` header) shows `"status": "approved"`.