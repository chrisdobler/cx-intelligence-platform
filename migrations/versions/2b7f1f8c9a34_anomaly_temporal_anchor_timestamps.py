"""anomaly temporal anchor timestamps

Revision ID: 2b7f1f8c9a34
Revises: 9c31f0aa54e7
Create Date: 2026-07-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "2b7f1f8c9a34"
down_revision = "9c31f0aa54e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "anomalies", sa.Column("observation_date", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "anomalies", sa.Column("baseline_date", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("anomalies", "baseline_date")
    op.drop_column("anomalies", "observation_date")
