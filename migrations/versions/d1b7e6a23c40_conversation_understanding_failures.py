"""conversation understanding failures

Revision ID: d1b7e6a23c40
Revises: 47c84e7640e2
Create Date: 2026-07-04 09:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1b7e6a23c40"
down_revision = "47c84e7640e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_understanding_failures",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=True),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("failure_category", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"]),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    op.create_index(
        "ix_conversation_understanding_failures_day_last_failed_at",
        "conversation_understanding_failures",
        ["day", "last_failed_at"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_understanding_failures_pipeline_run_id",
        "conversation_understanding_failures",
        ["pipeline_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_understanding_failures_pipeline_run_id",
        table_name="conversation_understanding_failures",
    )
    op.drop_index(
        "ix_conversation_understanding_failures_day_last_failed_at",
        table_name="conversation_understanding_failures",
    )
    op.drop_table("conversation_understanding_failures")
