"""Add chat messages and convos

Revision ID: 675268601278
Revises: 0173e14719ab
Create Date: 2025-05-14 10:38:19.130649

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import func

# revision identifiers, used by Alembic.
revision: str = "675268601278"
down_revision: Union[str, None] = "0173e14719ab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create chat_convos table
    op.create_table(
        "chat_convos",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("convo_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column(
            "project_state_id", sa.Uuid(), sa.ForeignKey("project_states.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime, server_default=func.now(), nullable=False),
    )

    # Create chat_messages table
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("convo_id", sa.Uuid(), sa.ForeignKey("chat_convos.convo_id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=func.now(), nullable=False),
        sa.Column("message_type", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("prev_message_id", sa.Uuid(), sa.ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("chat_messages")
    op.drop_table("chat_convos")
