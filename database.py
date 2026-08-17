"""
database.py
------------
Persistence layer for the HITL Approval Service.

Uses SQLAlchemy ORM and supports PostgreSQL for production
and SQLite for local development/testing.
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
    Stores a human-in-the-loop approval request.
    """

    __tablename__ = "approval_requests"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    request_id = Column(
        String(36),
        unique=True,
        nullable=False,
        index=True,
    )

    # Only the SHA-256 hash is stored.
    token_hash = Column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )

    # AI-generated response waiting for human approval.
    ai_response = Column(
        Text,
        nullable=False,
    )

    reviewer_email = Column(
        String(320),
        nullable=False,
    )

    status = Column(
        Enum(
            ApprovalStatus,
            native_enum=False,
            length=16,
        ),
        nullable=False,
        default=ApprovalStatus.PENDING,
        index=True,
    )

    created_at = Column(
        DateTime,
        nullable=False,
        default=dt.datetime.utcnow,
    )

    expires_at = Column(
        DateTime,
        nullable=False,
    )

    reviewed_at = Column(
        DateTime,
        nullable=True,
    )

    reviewer_comment = Column(
        Text,
        nullable=True,
    )

    # Configuration for optional reviewer fields.
    approver_input_schema = Column(
        JSON,
        nullable=True,
        default=list,
    )

    # Values entered by the reviewer.
    approver_inputs = Column(
        JSON,
        nullable=True,
        default=dict,
    )

    # Information required to resume the workflow.
    workflow_metadata = Column(
        JSON,
        nullable=True,
        default=dict,
    )

    email_subject = Column(
        String(500),
        nullable=True,
    )


# ---------------------------------------------------------
# Database configuration
# ---------------------------------------------------------

DATABASE_URL = settings.database_url

# SQLite requires this option.
# PostgreSQL does not.
connect_args = {}

if DATABASE_URL.startswith("sqlite"):
    connect_args = {
        "check_same_thread": False
    }


# ---------------------------------------------------------
# SQLAlchemy engine
# ---------------------------------------------------------

engine_kwargs = {
    "connect_args": connect_args,
}


# Production PostgreSQL connection pooling
if DATABASE_URL.startswith("postgresql"):
    engine_kwargs.update(
        {
            "pool_pre_ping": True,
            "pool_recycle": 1800,
        }
    )


engine = create_engine(
    DATABASE_URL,
    **engine_kwargs,
)


# ---------------------------------------------------------
# Session factory
# ---------------------------------------------------------

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


# ---------------------------------------------------------
# Database initialization
# ---------------------------------------------------------

def init_db() -> None:
    """
    Create database tables if they don't already exist.

    This is fine for initial deployment/testing.
    For future production schema changes, use Alembic migrations.
    """
    Base.metadata.create_all(bind=engine)


def get_session() -> Session:
    """
    Return a new database session.
    """
    return SessionLocal()