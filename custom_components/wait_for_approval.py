"""
wait_for_approval.py
---------------------
Langflow custom component implementing a Human-in-the-Loop approval gate.

READ THIS FIRST -- how "waiting" actually works
=================================================
A Langflow custom component's `build`/output methods run *inside a single,
bounded flow execution*. There is no supported mechanism in Langflow (as of
this writing) for a component to suspend that execution indefinitely and be
woken back up later by an unrelated inbound HTTP request -- Langflow does
not persist a "paused Python stack frame" the way e.g. LangGraph persists
checkpoints. See README.md's "Architectural Analysis" section for the full
reasoning and links.

Because of that, this component supports two honest modes, chosen with the
`wait_mode` input:

1. "Fire-and-forget" (recommended, the actual async pattern):
   The component creates the approval request in the external HITL
   Approval Service, sends the email, and immediately returns via the
   `Pending / Info` output with status="pending". The flow run ends here.
   When the reviewer later clicks Approve/Reject (minutes, hours, or days
   from now), the Approval Service records the decision and -- if you
   enabled LANGFLOW_CONTINUATION_ENABLED on that service -- calls a
   *second, continuation* Langflow flow via the Run API, passing it the
   decision. That continuation flow is where your real "Approved" /
   "Rejected" branches live. This is the pattern to use for anything that
   might take longer than a browser/API request is willing to stay open.

2. "Poll (blocking, short waits only)":
   The component polls the Approval Service's status endpoint in a loop,
   inside the current flow run, up to `max_wait_seconds`. If a decision
   arrives in time, the Approved/Rejected output fires synchronously in
   *this* run -- no continuation flow needed. This only makes sense for
   short human turnarounds (the reviewer is expected to respond in
   seconds/minutes) and only works if whatever is calling this Langflow
   flow (a script, a queue worker, a long-timeout API client) is willing to
   keep the HTTP connection/process open that whole time. It will NOT
   survive a Langflow restart -- if Langflow restarts mid-poll, the run is
   gone and the approval request will simply sit PENDING in the Approval
   Service until it expires, unless something else polls it later.

Do not use a plain blocking `while not approved: sleep()` against your own
in-process state -- that's what this component explicitly avoids, per the
design brief. All state lives in the external Approval Service's database,
which is what actually survives Langflow restarts.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

# NOTE on Langflow versions: as of Langflow 1.7+, the import path `lfx.custom` /
# `lfx.io` / `lfx.schema` replaces `langflow.custom` / `langflow.io` /
# `langflow.schema`, but per Langflow's own docs the old `langflow.*` paths
# remain compatible and work the same way. If your Langflow install only
# exposes the `lfx` namespace, swap the two import blocks below for:
#   from lfx.custom import Component
#   from lfx.io import (...)
#   from lfx.schema import Data
#   from lfx.schema.message import Message
from langflow.custom import Component
from langflow.io import (
    BoolInput,
    DropdownInput,
    IntInput,
    MultilineInput,
    Output,
    SecretStrInput,
    StrInput,
    TableInput,
)
from langflow.schema import Data
from langflow.schema.message import Message


class WaitForApprovalComponent(Component):
    display_name = "Wait for Approval"
    description = (
        "Send an AI response to a human reviewer by email and route the flow based on "
        "their Approve/Reject decision, via an external HITL Approval Service."
    )
    icon = "user-check"
    name = "WaitForApproval"

    inputs = [
        StrInput(
            name="ai_response",
            display_name="AI Response",
            info=(
                "The AI-generated response that needs human review. Connect this to an "
                "Agent/LLM output. Accepts a Message, Data, or plain string -- all are "
                "coerced to text."
            ),
            required=True,
            input_types=["Message", "Data", "str"],
        ),
        StrInput(
            name="reviewer_email",
            display_name="Reviewer Email",
            info="Email address of the person who must review this response.",
            required=True,
        ),
        StrInput(
            name="email_subject",
            display_name="Email Subject",
            value="Approval Required - AI Response Review",
        ),
        MultilineInput(
            name="approval_message",
            display_name="Approval Message",
            value="Please review the AI-generated response and choose Approve or Reject.",
        ),
        TableInput(
            name="approver_inputs",
            display_name="Approver Inputs",
            info=(
                "Optional extra fields the reviewer fills in before deciding (e.g. a "
                "comment). Leave empty for a plain Approve/Reject decision with no form."
            ),
            table_schema=[
                {"name": "field_name", "display_name": "Field name", "type": "str"},
                {"name": "label", "display_name": "Label", "type": "str"},
                {"name": "type", "display_name": "Type (text/number/boolean/select)", "type": "str"},
                {"name": "options", "display_name": "Options (comma-separated, for select)", "type": "str"},
                {"name": "description", "display_name": "Description", "type": "str"},
                {"name": "required", "display_name": "Required", "type": "boolean"},
            ],
            value=[],
        ),
        StrInput(
            name="approval_service_url",
            display_name="Approval Service URL",
            info="Base URL of the HITL Approval Service, e.g. https://approvals.yourdomain.com",
            required=True,
        ),
        SecretStrInput(
            name="internal_api_key",
            display_name="Internal API Key",
            info="Must match INTERNAL_API_KEY configured on the Approval Service. Never sent to reviewers.",
            required=True,
        ),
        IntInput(
            name="ttl_minutes",
            display_name="Expiration (minutes)",
            value=1440,
            info="How long the approval link stays valid before it auto-expires.",
            advanced=True,
        ),
        DropdownInput(
            name="wait_mode",
            display_name="Wait Mode",
            options=["Fire-and-forget (recommended)", "Poll (blocking, short waits only)"],
            value="Fire-and-forget (recommended)",
            info="See component docstring / README for the difference. Fire-and-forget is correct for real async approvals.",
        ),
        IntInput(
            name="max_wait_seconds",
            display_name="Max Wait Seconds (Poll mode only)",
            value=300,
            advanced=True,
        ),
        IntInput(
            name="poll_interval_seconds",
            display_name="Poll Interval Seconds (Poll mode only)",
            value=5,
            advanced=True,
        ),
        BoolInput(
            name="fail_on_email_error",
            display_name="Fail Flow if Email Send Fails",
            value=False,
            advanced=True,
            info="If false (default), the approval request is still created even if the email fails to send, and a warning is attached to the output so you can resend the link manually.",
        ),
    ]

    outputs = [
        Output(display_name="Approved", name="approved", method="run_approved", group_outputs=True),
        Output(display_name="Rejected", name="rejected", method="run_rejected", group_outputs=True),
        Output(display_name="Pending / Info", name="pending", method="run_pending", group_outputs=True),
    ]

    # -- internal helpers -----------------------------------------------

    def _text_of(self, value: Any) -> str:
        """Coerce Message / Data / str into plain text."""
        if isinstance(value, Message):
            return value.text or ""
        if isinstance(value, Data):
            return value.get_text() if hasattr(value, "get_text") else str(value.data)
        if isinstance(value, dict) and "text" in value:
            return str(value["text"])
        return str(value)

    def _build_approver_schema(self) -> list[dict]:
        rows = self.approver_inputs or []
        schema = []
        for row in rows:
            name = (row.get("field_name") or "").strip()
            if not name:
                continue
            field_type = (row.get("type") or "text").strip().lower()
            if field_type not in ("text", "number", "boolean", "select"):
                field_type = "text"
            options = [o.strip() for o in (row.get("options") or "").split(",") if o.strip()]
            schema.append(
                {
                    "name": name,
                    "label": row.get("label") or name,
                    "type": field_type,
                    "options": options,
                    "description": row.get("description") or "",
                    "required": bool(row.get("required", False)),
                }
            )
        return schema

    def _headers(self) -> dict:
        return {"X-Internal-Api-Key": self.internal_api_key, "Content-Type": "application/json"}

    def _create_request(self) -> dict:
        base_url = self.approval_service_url.rstrip("/")
        text = self._text_of(self.ai_response)
        body = {
            "ai_response": text,
            "reviewer_email": self.reviewer_email,
            "email_subject": self.email_subject,
            "approval_message": self.approval_message,
            "ttl_minutes": self.ttl_minutes,
            "approver_input_schema": self._build_approver_schema(),
            "workflow_metadata": {
                "flow_id": getattr(self, "flow_id", None),
                "session_id": getattr(self, "session_id", None),
            },
        }
        with httpx.Client(timeout=20) as client:
            resp = client.post(f"{base_url}/internal/approval-requests", json=body, headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def _get_status(self, request_id: str) -> dict:
        base_url = self.approval_service_url.rstrip("/")
        with httpx.Client(timeout=20) as client:
            resp = client.get(f"{base_url}/internal/approval-requests/{request_id}", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    def _resolve(self) -> dict:
        """Does the actual work exactly once per flow run, regardless of how
        many of the three output methods Langflow calls."""
        if getattr(self, "_resolved_cache", None) is not None:
            return self._resolved_cache

        original_text = self._text_of(self.ai_response)

        try:
            created = self._create_request()
        except httpx.HTTPStatusError as e:
            result = {
                "status": "error",
                "error": f"Approval Service rejected the request: {e.response.status_code} {e.response.text}",
                "response": original_text,
            }
            self._resolved_cache = result
            return result
        except httpx.RequestError as e:
            result = {
                "status": "error",
                "error": f"Could not reach Approval Service at {self.approval_service_url}: {e}",
                "response": original_text,
            }
            self._resolved_cache = result
            return result

        request_id = created["request_id"]
        result = {
            "status": "pending",
            "request_id": request_id,
            "response": original_text,
            "reviewer_email": self.reviewer_email,
            "review_url_note": "Only the reviewer's emailed link is valid; the review URL is not exposed here for security.",
            "email_sent": created.get("email_sent", False),
        }
        if not created.get("email_sent") and self.fail_on_email_error:
            result["status"] = "error"
            result["error"] = "Approval request was created but the notification email failed to send."

        if self.wait_mode.startswith("Poll"):
            deadline = time.monotonic() + max(1, self.max_wait_seconds)
            interval = max(1, self.poll_interval_seconds)
            while time.monotonic() < deadline:
                status_payload = self._get_status(request_id)
                if status_payload["status"] in ("approved", "rejected"):
                    result.update(
                        {
                            "status": status_payload["status"],
                            "reviewer_comment": status_payload.get("reviewer_comment"),
                            "approver_inputs": status_payload.get("approver_inputs") or {},
                            "reviewed_at": status_payload.get("reviewed_at"),
                        }
                    )
                    break
                if status_payload["status"] == "expired":
                    result["status"] = "expired"
                    break
                time.sleep(interval)
            else:
                result["status"] = "timeout"
                result["note"] = (
                    "Poll wait exceeded max_wait_seconds while the request is still pending "
                    "in the Approval Service. It has NOT expired there -- switch to "
                    "Fire-and-forget mode with a continuation flow if approvals routinely "
                    "take longer than this."
                )

        self._resolved_cache = result
        return result

    # -- outputs ----------------------------------------------------------

    def run_approved(self) -> Data:
        result = self._resolve()
        if result["status"] != "approved":
            self.stop("approved")
            return Data(data={})
        self.status = f"Approved (request_id={result['request_id']})"
        return Data(data=result)

    def run_rejected(self) -> Data:
        result = self._resolve()
        if result["status"] != "rejected":
            self.stop("rejected")
            return Data(data={})
        self.status = f"Rejected (request_id={result['request_id']})"
        return Data(data=result)

    def run_pending(self) -> Data:
        result = self._resolve()
        if result["status"] in ("approved", "rejected"):
            self.stop("pending")
            return Data(data={})
        self.status = f"{result['status']} (request_id={result.get('request_id', 'n/a')})"
        return Data(data=result)
