"""llm call observations

Revision ID: b7c4e661ad2f
Revises: 645598213ba7
Create Date: 2026-07-03 16:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c4e661ad2f"
down_revision = "645598213ba7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_call_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("total_seconds", sa.Float(), nullable=False),
        sa.Column("load_seconds", sa.Float(), nullable=False),
        sa.Column("prompt_seconds", sa.Float(), nullable=False),
        sa.Column("llm_seconds", sa.Float(), nullable=False),
        sa.Column("persist_seconds", sa.Float(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("prompt_characters", sa.Integer(), nullable=False),
        sa.Column("issue_count", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_llm_call_observations_pipeline_run_id",
        "llm_call_observations",
        ["pipeline_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_call_observations_conversation_id",
        "llm_call_observations",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_llm_call_observations_total_seconds",
        "llm_call_observations",
        ["total_seconds"],
        unique=False,
    )
    op.create_index(
        "ix_llm_call_observations_llm_seconds",
        "llm_call_observations",
        ["llm_seconds"],
        unique=False,
    )
    op.create_index(
        "ix_llm_call_observations_started_at",
        "llm_call_observations",
        ["started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_llm_call_observations_started_at", table_name="llm_call_observations")
    op.drop_index("ix_llm_call_observations_llm_seconds", table_name="llm_call_observations")
    op.drop_index("ix_llm_call_observations_total_seconds", table_name="llm_call_observations")
    op.drop_index("ix_llm_call_observations_conversation_id", table_name="llm_call_observations")
    op.drop_index(
        "ix_llm_call_observations_pipeline_run_id", table_name="llm_call_observations"
    )
    op.drop_table("llm_call_observations")
