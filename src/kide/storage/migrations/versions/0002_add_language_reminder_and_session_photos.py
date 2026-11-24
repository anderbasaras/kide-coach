"""add language, reminder and session photos

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_photos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("file_unique_id", sa.String(length=100), nullable=False),
        sa.Column("media_group_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["training_sessions.id"],
            name=op.f("fk_session_photos_session_id_training_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_photos")),
        sa.UniqueConstraint(
            "session_id", "file_unique_id", name=op.f("uq_session_photos_session_id_file_unique_id")
        ),
    )
    with op.batch_alter_table("session_photos", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_session_photos_session_id"), ["session_id"], unique=False
        )

    with op.batch_alter_table("daily_checkins", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reminder_sent_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("language", sa.String(length=10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("language")

    with op.batch_alter_table("daily_checkins", schema=None) as batch_op:
        batch_op.drop_column("reminder_sent_at")

    with op.batch_alter_table("session_photos", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_session_photos_session_id"))

    op.drop_table("session_photos")
