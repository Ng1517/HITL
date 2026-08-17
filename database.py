"""
database.py
------------
Persistence layer for the HITL Approval Service.

Design goals:
- Replaceable storage: everything goes through SQLAlchemy's ORM, so swapping
  SQLite for PostgreSQL is a one-line change to DATABASE_URL (see config.py).
- We never store the raw approval token. Only a SHA-256 hash of it
  (`token_hash`) is persisted, so a database leak does not hand out working
  approval links.
- Status transitions are enforced both at the application layer (see
  crud.py's atomic UPDATE ... WHERE status='pending') and reflected here via
  a plain string column with a small set of allowed values.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import settings


class Base(DeclarativeBase):
    pass


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalRequest(Base):
    """
    Conceptual table from the design doc, implemented as an ORM model.

    id                -> primary key (internal, opaque, safe to log)
    request_id        -> public-facing UUID, safe to show to users/UI
    token_hash         -> SHA-256 hex digest of the secret approval token
    ai_response        -> the text that needs review (already sanitized
                           on write is NOT assumed here; sanitization for
                           HTML rendering happens at render time, defense
                           in depth)
    reviewer_email      -> who the request was sent to
    status              -> pending | approved | rejected | expired
    created_at          -> UTC timestamp
    expires_at          -> UTC timestamp; requests older than this are
                            treated as EXPIRED even if the DB row still
                            says 'pending' (lazy expiry), and a
                            background sweeper also flips the status.
    reviewed_at         -> UTC timestamp of the decision, if any
    reviewer_comment    -> free-text comment left by the reviewer
    approver_input_schema -> JSON schema (list of field defs) describing
                              any extra fields configured on the component
    approver_inputs     -> JSON object of values the reviewer submitted
                            for those fields
    workflow_metadata   -> JSON blob the Langflow component can use to
                            carry whatever context is needed to resume the
                            flow (e.g. flow_id, session_id, tweaks) -- see
                            "Continuation Flow" pattern in README.md
    """

    __tablename__ = "approval_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(36), unique=True, nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)

    ai_response = Column(Text, nullable=False)
    reviewer_email = Column(String(320), nullable=False)

    status = Column(
        Enum(ApprovalStatus, native_enum=False, length=16),
        nullable=False,
        default=ApprovalStatus.PENDING,
        index=True,
    )

    created_at = Column(DateTime, nullable=False, default=dt.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)

    reviewer_comment = Column(Text, nullable=True)
    approver_input_schema = Column(JSON, nullable=True, default=list)
    approver_inputs = Column(JSON, nullable=True, default=dict)

    workflow_metadata = Column(JSON, nullable=True, default=dict)

    email_subject = Column(String(500), nullable=True)


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_session() -> Session:
    return SessionLocal()
