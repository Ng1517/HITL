"""
webhook.py
----------
This is the piece that actually "resumes the workflow" after a human
decision arrives. See README.md for the full architectural explanation of
why this has to be a *separate* call into Langflow's Run API rather than
the original flow run somehow "waking back up".

Two supported patterns, both driven by config.settings:

1. langflow_continuation_enabled=False (default): this service only
   updates its own database. Whatever is polling
   GET /internal/approval-requests/{id} (the Langflow component in "Poll"
   mode, or your own orchestrator) will observe the new status on its next
   poll. Nothing is pushed.

2. langflow_continuation_enabled=True: as soon as a decision is recorded,
   this service POSTs the decision payload to LANGFLOW_API_URL (a
   Langflow "Run flow" API endpoint for a *second, continuation* flow that
   you build in Langflow -- see README "Continuation Flow" pattern). That
   flow receives the AI response, the decision, the reviewer's comment and
   any approver inputs as its input, and does whatever the "Approved" /
   "Rejected" branch of your original flow was supposed to do.
"""

from __future__ import annotations

import logging

import httpx

from config import settings
from database import ApprovalRequest, ApprovalStatus

logger = logging.getLogger("approval_service.webhook")


def notify_continuation(row: ApprovalRequest) -> None:
    if not settings.langflow_continuation_enabled:
        return
    if not settings.langflow_api_url:
        logger.warning("langflow_continuation_enabled=True but LANGFLOW_API_URL is not set; skipping")
        return

    status = row.status.value if isinstance(row.status, ApprovalStatus) else row.status
    payload = {
        "input_value": {
            "status": status,
            "request_id": row.request_id,
            "response": row.ai_response,
            "reviewer_email": row.reviewer_email,
            "reviewer_comment": row.reviewer_comment,
            "approver_inputs": row.approver_inputs or {},
            "workflow_metadata": row.workflow_metadata or {},
        },
        "output_type": "chat",
        "input_type": "chat",
    }
    headers = {"Content-Type": "application/json"}
    if settings.langflow_api_key:
        headers["x-api-key"] = settings.langflow_api_key

    try:
        resp = httpx.post(settings.langflow_api_url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        logger.info("Continuation flow triggered for request_id=%s (status=%s)", row.request_id, status)
    except Exception:
        # We deliberately do not raise: the decision is already durably
        # recorded in the database. A failed continuation call just means
        # the downstream flow wasn't auto-triggered this time; you can
        # retry by re-reading /internal/approval-requests/{request_id} or
        # by adding a retry queue (see README "Production hardening").
        logger.exception("Failed to notify continuation flow for request_id=%s", row.request_id)
