"""evaluation runs and token usage

Revision ID: a4d9e2f7c101
Revises: f1b2c3d4e5f6
Create Date: 2026-07-04 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a4d9e2f7c101"
down_revision = "f1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_call_observations", sa.Column("prompt_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "llm_call_observations", sa.Column("output_tokens", sa.Integer(), nullable=True)
    )
    op.add_column(
        "llm_call_observations", sa.Column("total_tokens", sa.Integer(), nullable=True)
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=True),
        sa.Column("dataset_version", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("understanding_prompt_version", sa.String(), nullable=False),
        sa.Column("resolution_prompt_version", sa.String(), nullable=False),
        sa.Column("suites", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("passed_cases", sa.Integer(), nullable=False),
        sa.Column("pass_rate", sa.Float(), nullable=False),
        sa.Column("regression_count", sa.Integer(), nullable=False),
        sa.Column("retrieval_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("grounding_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evaluation_runs_started_at",
        "evaluation_runs",
        ["started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_runs_started_at", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_column("llm_call_observations", "total_tokens")
    op.drop_column("llm_call_observations", "output_tokens")
    op.drop_column("llm_call_observations", "prompt_tokens")
