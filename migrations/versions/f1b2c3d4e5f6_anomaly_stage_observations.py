"""anomaly stage observations

Revision ID: f1b2c3d4e5f6
Revises: 2b7f1f8c9a34
Create Date: 2026-07-04 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f1b2c3d4e5f6"
down_revision = "2b7f1f8c9a34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "anomaly_stage_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=True),
        sa.Column("step", sa.String(), nullable=False),
        sa.Column("day", sa.Integer(), nullable=True),
        sa.Column("issue", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("total_seconds", sa.Float(), nullable=False),
        sa.Column("baseline_issue_count", sa.Integer(), nullable=False),
        sa.Column("current_issue_count", sa.Integer(), nullable=False),
        sa.Column("anomalies_detected", sa.Integer(), nullable=False),
        sa.Column("alert_count", sa.Integer(), nullable=False),
        sa.Column("fallback_count", sa.Integer(), nullable=False),
        sa.Column("delivered_count", sa.Integer(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_anomaly_stage_observations_pipeline_run_id",
        "anomaly_stage_observations",
        ["pipeline_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_stage_observations_step",
        "anomaly_stage_observations",
        ["step"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_stage_observations_day",
        "anomaly_stage_observations",
        ["day"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_stage_observations_total_seconds",
        "anomaly_stage_observations",
        ["total_seconds"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_stage_observations_started_at",
        "anomaly_stage_observations",
        ["started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_anomaly_stage_observations_started_at",
        table_name="anomaly_stage_observations",
    )
    op.drop_index(
        "ix_anomaly_stage_observations_total_seconds",
        table_name="anomaly_stage_observations",
    )
    op.drop_index("ix_anomaly_stage_observations_day", table_name="anomaly_stage_observations")
    op.drop_index("ix_anomaly_stage_observations_step", table_name="anomaly_stage_observations")
    op.drop_index(
        "ix_anomaly_stage_observations_pipeline_run_id",
        table_name="anomaly_stage_observations",
    )
    op.drop_table("anomaly_stage_observations")
