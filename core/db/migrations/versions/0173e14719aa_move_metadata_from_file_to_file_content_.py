"""move metadata from file to file content table

Revision ID: 0173e14719aa
Revises: 3968d770dced
Create Date: 2025-05-15 15:33:03.084670

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0173e14719aa"
down_revision: Union[str, None] = "3968d770dced"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add meta column to file_contents
    with op.batch_alter_table("file_contents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("meta", sa.JSON(), server_default="{}", nullable=False))

    # Copy data from files.meta to file_contents.meta
    op.execute("""
        UPDATE file_contents
        SET meta = files.meta
        FROM files
        WHERE file_contents.id = files.content_id
    """)

    # Drop meta column from files
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.drop_column("meta")


def downgrade() -> None:
    # Add meta column back to files
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.add_column(sa.Column("meta", sa.JSON(), server_default="{}", nullable=False))

    # Copy data from file_contents.meta back to files.meta
    op.execute("""
        UPDATE files
        SET meta = file_contents.meta
        FROM file_contents
        WHERE files.content_id = file_contents.id
    """)

    # Drop meta column from file_contents
    with op.batch_alter_table("file_contents", schema=None) as batch_op:
        batch_op.drop_column("meta")
