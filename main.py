"""
main.py
-------
The HITL Approval Service.

Why this service exists (see README.md "Architecture" for the full
discussion): a Langflow custom component executes inside a single,
bounded flow run. It cannot stay alive in memory for minutes/hours/days
waiting for a reviewer to click a link in an email, and it cannot receive
an inbound HTTP request itself. So the actual "waiting" and "receiving the
click" has to live in a normal, always-on web service -- this one.

Endpoints
---------
Internal (called by the Langflow component; protected by X-Internal-Api-Key):
  POST /internal/approval-requests            create a request + send email
  GET  /internal/approval-requests/{request_id}  poll current status

Public (opened by the human reviewer from the email; no auth beyond the
unguessable token, rate-limited, single-use):
  GET  /approval/{token}                       full review page, shows the
                                                AI response and both
                                                Approve/Reject options (not
                                                linked from the default
                                                email, kept for manual/
                                                shared links)
  GET  /approval/{token}/approve                confirm page pre-selected
                                                for Approve: shows the AI
                                                response, any approver
                                                input fields, and a single
                                                "Confirm Approval" button.
                                                Read-only -- does NOT record
                                                a decision by itself.
  GET  /approval/{token}/reject                 same, pre-selected Reject
  POST /approval/{token}/decide                 the actual state change.
                                                Called by the confirm
                                                button's form (or the full
                                                review page's form).

Design note on GET /approve and /reject: these are intentionally
side-effect-free. They only render a page with one confirm button that the
human must click; that click is what issues the state-changing POST above.
This matters because mail clients and corporate security gateways (Outlook
Safe Links, Proofpoint, Mimecast, Defender for Office 365, etc.) routinely
pre-fetch every link in an incoming email to scan it for malware, before a
human ever opens the message. If a bare GET performed the approval, those
automated scans could silently approve or reject a request with nobody
involved -- the same failure mode that has burned "one-click unsubscribe"
links industry-wide. See README.md "Architecture" for the full picture.

Operational:
  GET  /healthz
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.background import BackgroundScheduler

import crud
from config import settings
from database import ApprovalStatus, get_session, init_db
from email_service import send_approval_email
from security import (
    SlidingWindowRateLimiter,
    compare_api_key,
    hash_token,
    sanitize_for_html,
)
from webhook import notify_continuation

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("approval_service")

app = FastAPI(title="Langflow HITL Approval Service", version="1.0.0")
templates = Jinja2Templates(directory="templates")
rate_limiter = SlidingWindowRateLimiter(max_requests=settings.rate_limit_per_minute, window_seconds=60)

_scheduler: BackgroundScheduler | None = None


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    global _scheduler
    _scheduler = BackgroundScheduler(daemon=True)

    def _sweep():
        db = get_session()
        try:
            n = crud.sweep_expired(db)
            if n:
                logger.info("Expired %d stale approval request(s)", n)
        finally:
            db.close()

    _scheduler.add_job(_sweep, "interval", minutes=5, id="sweep_expired")
    _scheduler.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    if _scheduler:
        _scheduler.shutdown(wait=False)


def _require_internal_auth(x_internal_api_key: str | None) -> None:
    if not compare_api_key(x_internal_api_key or "", settings.internal_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Api-Key")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limit_or_429(request: Request) -> None:
    if not rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many requests, slow down.")


# ---------------------------------------------------------------------------
# Internal API (Langflow component -> this service)
# ---------------------------------------------------------------------------

@app.post("/internal/approval-requests")
def create_approval_request(
    payload: dict,
    x_internal_api_key: str | None = Header(default=None),
):
    _require_internal_auth(x_internal_api_key)

    ai_response = payload.get("ai_response", "")
    reviewer_email = payload.get("reviewer_email")
    email_subject = payload.get("email_subject") or "Approval Required - AI Response Review"
    approval_message = payload.get(
        "approval_message", "Please review the AI-generated response and choose Approve or Reject."
    )
    ttl_minutes = int(payload.get("ttl_minutes") or settings.default_ttl_minutes)
    approver_input_schema = payload.get("approver_input_schema") or []
    workflow_metadata = payload.get("workflow_metadata") or {}

    if not reviewer_email:
        raise HTTPException(status_code=400, detail="reviewer_email is required")
    if not ai_response:
        raise HTTPException(status_code=400, detail="ai_response is required")

    db = get_session()
    try:
        created = crud.create_approval_request(
            db,
            ai_response=ai_response,
            reviewer_email=reviewer_email,
            email_subject=email_subject,
            ttl_minutes=ttl_minutes,
            approver_input_schema=approver_input_schema,
            workflow_metadata=workflow_metadata,
        )
    finally:
        db.close()

    approve_url = f"{settings.public_base_url}/approval/{created.token}/approve"
    reject_url = f"{settings.public_base_url}/approval/{created.token}/reject"

    try:
        html_body = templates.get_template("email_approval.html").render(
            approval_message=approval_message,
            ai_response_escaped=sanitize_for_html(ai_response),
            approve_url=approve_url,
            reject_url=reject_url,
            has_approver_inputs=bool(approver_input_schema),
            request_id=created.request_id,
            expires_at=created.expires_at.isoformat(sep=" ", timespec="minutes"),
        )
        text_body = (
            f"{approval_message}\n\nAI Response:\n{ai_response}\n\n"
            f"Approve: {approve_url}\nReject: {reject_url}\n\nRequest ID: {created.request_id}\n"
            f"Expires: {created.expires_at.isoformat(sep=' ', timespec='minutes')} UTC"
        )
        send_approval_email(reviewer_email, email_subject, html_body, text_body)
        email_sent = True
    except Exception:
        # We deliberately do NOT fail the whole request if email sending
        # fails -- the approval request still exists and is reachable by
        # request_id/token, and Langflow can surface a warning. See README
        # "What happens if the email fails to send".
        logger.exception("Email send failed for request_id=%s", created.request_id)
        email_sent = False

    return JSONResponse(
        {
            "request_id": created.request_id,
            "status": "pending",
            "expires_at": created.expires_at.isoformat(),
            "email_sent": email_sent,
            "review_url": approve_url,
        }
    )


@app.get("/internal/approval-requests/{request_id}")
def get_approval_status(request_id: str, x_internal_api_key: str | None = Header(default=None)):
    _require_internal_auth(x_internal_api_key)
    db = get_session()
    try:
        row = crud.get_by_request_id(db, request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown request_id")
        return {
            "request_id": row.request_id,
            "status": row.status.value if isinstance(row.status, ApprovalStatus) else row.status,
            "reviewer_comment": row.reviewer_comment,
            "approver_inputs": row.approver_inputs,
            "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            "expires_at": row.expires_at.isoformat(),
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Public API (reviewer clicks the email link)
# ---------------------------------------------------------------------------

def _render_result(request: Request, *, kind: str, request_id: str) -> HTMLResponse:
    presets = {
        "approved": ("ok", "\u2713", "Approval Successful", "This AI response has been approved."),
        "rejected": ("bad", "\u2715", "Response Rejected", "This AI response has been rejected."),
        "already_resolved": ("warn", "\u26a0", "Already Processed", "This approval request has already been resolved and cannot be changed."),
        "expired": ("warn", "\u23f0", "Link Expired", "This approval request has expired. Please ask the workflow owner to resend it."),
        "invalid": ("bad", "\u2715", "Invalid Link", "This approval link is invalid."),
    }
    icon_class, icon, title, message = presets[kind]
    html = templates.get_template("page_result.html").render(
        icon_class=icon_class, icon=icon, title=title, message=message, request_id=request_id
    )
    return HTMLResponse(html)


@app.get("/approval/{token}", response_class=HTMLResponse)
def review_page(token: str, request: Request):
    _rate_limit_or_429(request)
    db = get_session()
    try:
        row = crud.get_by_token_hash(db, hash_token(token))
        if row is None:
            return _render_result(request, kind="invalid", request_id="unknown")
        if row.status == ApprovalStatus.EXPIRED:
            return _render_result(request, kind="expired", request_id=row.request_id)
        if row.status != ApprovalStatus.PENDING:
            return _render_result(request, kind="already_resolved", request_id=row.request_id)

        html = templates.get_template("page_review.html").render(
            ai_response_escaped=sanitize_for_html(row.ai_response),
            approver_fields=row.approver_input_schema or [],
            submit_url=f"/approval/{token}/decide",
            request_id=row.request_id,
            expires_at=row.expires_at.isoformat(sep=" ", timespec="minutes"),
            error=None,
        )
        return HTMLResponse(html)
    finally:
        db.close()


@app.post("/approval/{token}/decide", response_class=HTMLResponse)
async def decide(token: str, request: Request):
    _rate_limit_or_429(request)

    form = await request.form()
    decision = form.get("decision")
    if decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision must be 'approve' or 'reject'")

    db = get_session()
    try:
        row = crud.get_by_token_hash(db, hash_token(token))
        if row is None:
            return _render_result(request, kind="invalid", request_id="unknown")
        if row.status == ApprovalStatus.EXPIRED:
            return _render_result(request, kind="expired", request_id=row.request_id)
        if row.status != ApprovalStatus.PENDING:
            return _render_result(request, kind="already_resolved", request_id=row.request_id)

        schema = row.approver_input_schema or []
        reviewer_comment = None
        approver_inputs: dict = {}
        errors = []

        for field in schema:
            name = field["name"]
            value = form.get(name, "")
            if field.get("required") and not value:
                errors.append(f"'{field.get('label', name)}' is required.")
                continue
            if field.get("type") == "boolean":
                approver_inputs[name] = value == "true" if value != "" else None
            elif field.get("type") == "number":
                approver_inputs[name] = float(value) if value != "" else None
            else:
                approver_inputs[name] = value
            if name == "reviewer_comment":
                reviewer_comment = value

        # A conventional 'reviewer_comment' field, if present in the schema,
        # is also mirrored onto the dedicated reviewer_comment column so it
        # is easy to query/report on independently of approver_inputs JSON.
        if reviewer_comment is None:
            reviewer_comment = form.get("reviewer_comment")

        if errors:
            html = templates.get_template("page_review.html").render(
                ai_response_escaped=sanitize_for_html(row.ai_response),
                approver_fields=schema,
                submit_url=f"/approval/{token}/decide",
                request_id=row.request_id,
                expires_at=row.expires_at.isoformat(sep=" ", timespec="minutes"),
                error=" ".join(errors),
            )
            return HTMLResponse(html, status_code=400)

        status = ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.REJECTED
        try:
            updated = crud.record_decision(
                db, row, decision=status, reviewer_comment=reviewer_comment, approver_inputs=approver_inputs
            )
        except crud.AlreadyResolvedError:
            return _render_result(request, kind="already_resolved", request_id=row.request_id)

        notify_continuation(updated)
        kind = "approved" if status == ApprovalStatus.APPROVED else "rejected"
        return _render_result(request, kind=kind, request_id=updated.request_id)
    finally:
        db.close()


@app.get("/approval/{token}/approve", response_class=HTMLResponse)
def confirm_approve_page(token: str, request: Request):
    return _render_confirm_page(token, request, ApprovalStatus.APPROVED)


@app.get("/approval/{token}/reject", response_class=HTMLResponse)
def confirm_reject_page(token: str, request: Request):
    return _render_confirm_page(token, request, ApprovalStatus.REJECTED)


def _render_confirm_page(token: str, request: Request, decision: ApprovalStatus) -> HTMLResponse:
    """Renders a page with exactly one confirm button for the given decision.
    Deliberately read-only (no state change) so that automated link
    prescanners hitting this GET cannot trigger an approval/rejection --
    only the human's subsequent click on the "Confirm ..." button, which
    issues the actual state-changing POST to /decide, can do that."""
    _rate_limit_or_429(request)
    db = get_session()
    try:
        row = crud.get_by_token_hash(db, hash_token(token))
        if row is None:
            return _render_result(request, kind="invalid", request_id="unknown")
        if row.status == ApprovalStatus.EXPIRED:
            return _render_result(request, kind="expired", request_id=row.request_id)
        if row.status != ApprovalStatus.PENDING:
            return _render_result(request, kind="already_resolved", request_id=row.request_id)

        decision_str = "approve" if decision == ApprovalStatus.APPROVED else "reject"
        switch_str = "reject" if decision == ApprovalStatus.APPROVED else "approve"
        html = templates.get_template("page_confirm.html").render(
            decision=decision_str,
            decision_label="Approval" if decision == ApprovalStatus.APPROVED else "Rejection",
            ai_response_escaped=sanitize_for_html(row.ai_response),
            approver_fields=row.approver_input_schema or [],
            submit_url=f"/approval/{token}/decide",
            switch_url=f"/approval/{token}/{switch_str}",
            request_id=row.request_id,
            expires_at=row.expires_at.isoformat(sep=" ", timespec="minutes"),
            error=None,
        )
        return HTMLResponse(html)
    finally:
        db.close()

@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": dt.datetime.utcnow().isoformat()}
