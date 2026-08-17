"""
crud.py
-------
All state transitions go through here, and every transition that changes
`status` is done as a single conditional UPDATE (`WHERE status = 'pending'`)
so that two concurrent requests (e.g. the reviewer double-clicking Approve,
or two people opening the same email -- see README "Two reviewers" scenario)
can never both "win". Exactly one commit will affect a row; the loser sees
0 rows updated and is told the request was already resolved.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.orm import Session

from database import ApprovalRequest, ApprovalStatus
from security import generate_token, hash_token


@dataclass
class CreatedRequest:
    request_id: str
    token: str  # raw token -- caller must use it once (put in email) then discard
    expires_at: dt.datetime


def create_approval_request(
    db: Session,
    *,
    ai_response: str,
    reviewer_email: str,
    email_subject: str | None,
    ttl_minutes: int,
    approver_input_schema: list[dict],
    workflow_metadata: dict,
) -> CreatedRequest:
    token = generate_token()
    now = dt.datetime.utcnow()
    row = ApprovalRequest(
        request_id=str(uuid.uuid4()),
        token_hash=hash_token(token),
        ai_response=ai_response,
        reviewer_email=reviewer_email,
        email_subject=email_subject,
        status=ApprovalStatus.PENDING,
        created_at=now,
        expires_at=now + dt.timedelta(minutes=ttl_minutes),
        approver_input_schema=approver_input_schema or [],
        approver_inputs={},
        workflow_metadata=workflow_metadata or {},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return CreatedRequest(request_id=row.request_id, token=token, expires_at=row.expires_at)


def _expire_if_needed(db: Session, row: ApprovalRequest) -> ApprovalRequest:
    """Lazy expiry: if we read a PENDING row whose TTL has passed, flip it
    to EXPIRED right now. A periodic sweeper (see main.py startup task) also
    does this proactively, but lazy expiry guarantees correctness even if
    the sweeper hasn't run yet or the process just restarted."""
    if row.status == ApprovalStatus.PENDING and dt.datetime.utcnow() > row.expires_at:
        result = db.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == row.id, ApprovalRequest.status == ApprovalStatus.PENDING)
            .values(status=ApprovalStatus.EXPIRED)
        )
        db.commit()
        if result.rowcount:
            row.status = ApprovalStatus.EXPIRED
    return row


def get_by_token_hash(db: Session, token_hash: str) -> ApprovalRequest | None:
    row = db.query(ApprovalRequest).filter(ApprovalRequest.token_hash == token_hash).first()
    if row is None:
        return None
    return _expire_if_needed(db, row)


def get_by_request_id(db: Session, request_id: str) -> ApprovalRequest | None:
    row = db.query(ApprovalRequest).filter(ApprovalRequest.request_id == request_id).first()
    if row is None:
        return None
    return _expire_if_needed(db, row)


class AlreadyResolvedError(Exception):
    """Raised when a decision is submitted for a request that is no longer PENDING."""

    def __init__(self, current_status: str):
        self.current_status = current_status
        super().__init__(f"Request already resolved with status={current_status}")


def record_decision(
    db: Session,
    row: ApprovalRequest,
    *,
    decision: ApprovalStatus,
    reviewer_comment: str | None,
    approver_inputs: dict,
) -> ApprovalRequest:
    assert decision in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)

    row = _expire_if_needed(db, row)
    if row.status != ApprovalStatus.PENDING:
        raise AlreadyResolvedError(current_status=row.status.value if isinstance(row.status, ApprovalStatus) else row.status)

    now = dt.datetime.utcnow()
    result = db.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.id == row.id, ApprovalRequest.status == ApprovalStatus.PENDING)
        .values(
            status=decision,
            reviewed_at=now,
            reviewer_comment=reviewer_comment,
            approver_inputs=approver_inputs,
        )
    )
    db.commit()

    if result.rowcount == 0:
        # Someone else won the race between our read and our write.
        db.refresh(row)
        raise AlreadyResolvedError(current_status=row.status.value if isinstance(row.status, ApprovalStatus) else row.status)

    db.refresh(row)
    return row


def sweep_expired(db: Session) -> int:
    result = db.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalStatus.PENDING, ApprovalRequest.expires_at < dt.datetime.utcnow())
        .values(status=ApprovalStatus.EXPIRED)
    )
    db.commit()
    return result.rowcount or 0
