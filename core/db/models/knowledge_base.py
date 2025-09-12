from copy import deepcopy
from typing import TYPE_CHECKING

from sqlalchemy import JSON, delete, distinct, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db.models.base import Base

if TYPE_CHECKING:
    from core.db.models import ProjectState


class KnowledgeBase(Base):
    """Model for storing project knowledge base.

    This model stores various pieces of project-related information:
    - pages: List of implemented frontend pages
    - apis: List of API endpoints and their implementation status
    - user_options: User configuration options for the project
    - utility_functions: List of utility functions with their status, input/output values
    """

    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pages: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    apis: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]")
    user_options: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    utility_functions: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]")

    # Relationships
    project_states: Mapped[list["ProjectState"]] = relationship(back_populates="knowledge_base", lazy="raise")

    def clone(self) -> "KnowledgeBase":
        """
        Clone the knowledge base.

        Creates a new KnowledgeBase instance with the same data but new ID.
        Used when the knowledge base needs to be modified to maintain immutability
        of previous states.
        """
        return KnowledgeBase(
            pages=deepcopy(self.pages),
            apis=deepcopy(self.apis),
            user_options=deepcopy(self.user_options),
            utility_functions=deepcopy(self.utility_functions),
        )

    @classmethod
    async def delete_orphans(cls, session: AsyncSession):
        """
        Delete KnowledgeBase objects that are not referenced by any ProjectState object.

        :param session: The database session.
        """
        from core.db.models import ProjectState

        await session.execute(
            delete(KnowledgeBase).where(~KnowledgeBase.id.in_(select(distinct(ProjectState.knowledge_base_id))))
        )
