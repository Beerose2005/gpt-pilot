"""vacuum database

Revision ID: 0173e14719ab
Revises: 69e50fdaf067
Create Date: 2025-05-15 15:33:03.084670

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0173e14719ab"
down_revision: Union[str, None] = "69e50fdaf067"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Get connection
    connection = op.get_bind()

    # Temporarily disable journal mode
    connection.execute(text("PRAGMA journal_mode = OFF"))

    # Run VACUUM
    connection.execute(text("VACUUM"))


def downgrade() -> None:
    # VACUUM is not reversible
    pass
