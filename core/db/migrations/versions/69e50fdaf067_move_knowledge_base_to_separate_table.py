"""move knowledge base to separate table

Revision ID: 69e50fdaf067
Revises: 0173e14719aa
Create Date: 2025-05-15 17:27:50.312917

"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import JSON, Column, Integer, MetaData, Table, insert, text
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = "69e50fdaf067"
down_revision: Union[str, None] = "0173e14719aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the new knowledge_bases table
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pages", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("apis", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("user_options", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("utility_functions", sa.JSON(), server_default="[]", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_bases")),
    )

    # Add knowledge_base_id column to project_states
    with op.batch_alter_table("project_states", schema=None) as batch_op:
        batch_op.add_column(sa.Column("knowledge_base_id", sa.Integer(), nullable=True))

    # Get connection for data migration
    connection = op.get_bind()

    # Create a table object for knowledge_bases
    metadata = MetaData()
    knowledge_bases = Table(
        "knowledge_bases",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("pages", JSON),
        Column("apis", JSON),
        Column("user_options", JSON),
        Column("utility_functions", JSON),
    )

    # Keep track of unique knowledge bases to avoid redundancy
    kb_cache = {}

    # Migrate data from old knowledge_base column to new table
    project_states = connection.execute(text("SELECT id, knowledge_base FROM project_states")).fetchall()
    for state_id, kb_data_str in project_states:
        if kb_data_str:
            try:
                # Parse the JSON string into a dictionary
                kb_data = json.loads(kb_data_str) if isinstance(kb_data_str, str) else kb_data_str
            except (json.JSONDecodeError, TypeError):
                # If parsing fails, use empty defaults
                kb_data = {}

            # Create a cache key from the knowledge base content
            cache_key = json.dumps(
                {
                    "pages": kb_data.get("pages", []),
                    "apis": kb_data.get("apis", []),
                    "user_options": kb_data.get("user_options", {}),
                    "utility_functions": kb_data.get("utility_functions", []),
                },
                sort_keys=True,
            )

            if cache_key not in kb_cache:
                # Insert new knowledge base record
                stmt = insert(knowledge_bases).values(
                    pages=kb_data.get("pages", []),
                    apis=kb_data.get("apis", []),
                    user_options=kb_data.get("user_options", {}),
                    utility_functions=kb_data.get("utility_functions", []),
                )
                result = connection.execute(stmt)
                kb_cache[cache_key] = result.inserted_primary_key[0]

            # Update project state to reference the knowledge base
            kb_id = kb_cache[cache_key]
            connection.execute(
                text("UPDATE project_states SET knowledge_base_id = :kb_id WHERE id = :state_id"),
                {"kb_id": kb_id, "state_id": state_id},
            )

    # Make knowledge_base_id not nullable and add foreign key constraint
    with op.batch_alter_table("project_states", schema=None) as batch_op:
        batch_op.alter_column("knowledge_base_id", nullable=False)
        batch_op.create_foreign_key(
            batch_op.f("fk_project_states_knowledge_base_id_knowledge_bases"),
            "knowledge_bases",
            ["knowledge_base_id"],
            ["id"],
        )
        batch_op.drop_column("knowledge_base")

    # Clean up llm_requests table
    op.execute("DELETE FROM llm_requests")


def downgrade() -> None:
    # Add back the knowledge_base column
    with op.batch_alter_table("project_states", schema=None) as batch_op:
        batch_op.add_column(sa.Column("knowledge_base", sqlite.JSON(), server_default=sa.text("'{}'"), nullable=False))

    # Get connection for data migration
    connection = op.get_bind()

    # Migrate data back from knowledge_bases table to project_states
    results = connection.execute(
        text("""
        SELECT ps.id, kb.pages, kb.apis, kb.user_options, kb.utility_functions
        FROM project_states ps
        JOIN knowledge_bases kb ON ps.knowledge_base_id = kb.id
    """)
    ).fetchall()

    for state_id, pages, apis, user_options, utility_functions in results:
        # Create the knowledge base data structure
        kb_data = {"pages": pages, "apis": apis, "user_options": user_options, "utility_functions": utility_functions}
        connection.execute(
            text("UPDATE project_states SET knowledge_base = :kb_data WHERE id = :state_id"),
            {"kb_data": json.dumps(kb_data), "state_id": state_id},
        )

    # Remove the knowledge_base_id column and knowledge_bases table
    with op.batch_alter_table("project_states", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_project_states_knowledge_base_id_knowledge_bases"), type_="foreignkey")
        batch_op.drop_column("knowledge_base_id")

    op.drop_table("knowledge_bases")
