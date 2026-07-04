"""knowledge documents

Revision ID: 9c31f0aa54e7
Revises: d1b7e6a23c40
Create Date: 2026-07-03 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision = "9c31f0aa54e7"
down_revision = "d1b7e6a23c40"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 3072


def upgrade() -> None:
    # The docker initdb script only enables pgvector in the dev database;
    # freshly created databases (e.g. the test database) need it here.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("issue", sa.String(), nullable=False),
        sa.Column("product", sa.String(), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("knowledge_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_documents_conversation_id",
        "knowledge_documents",
        ["conversation_id"],
        unique=False,
    )
    op.create_index("ix_knowledge_documents_issue", "knowledge_documents", ["issue"], unique=False)
    op.create_index(
        "ix_knowledge_documents_product", "knowledge_documents", ["product"], unique=False
    )
    # No vector index: pgvector HNSW/IVFFlat cap at 2000 dimensions and the
    # dataset is thousands of rows — a sequential scan is the simpler choice.


def downgrade() -> None:
    op.drop_index("ix_knowledge_documents_product", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_issue", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_conversation_id", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
