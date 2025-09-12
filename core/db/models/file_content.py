from typing import TYPE_CHECKING

from sqlalchemy import delete, distinct, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db.models import Base

if TYPE_CHECKING:
    from core.db.models import File


class FileContent(Base):
    __tablename__ = "file_contents"

    # ID and parent FKs
    id: Mapped[str] = mapped_column(primary_key=True)

    # Attributes
    content: Mapped[str] = mapped_column()
    meta: Mapped[dict] = mapped_column(default=dict, server_default="{}")

    # Relationships
    files: Mapped[list["File"]] = relationship(back_populates="content", lazy="raise")

    @classmethod
    async def store(cls, session: AsyncSession, hash: str, content: str, meta: dict = None) -> "FileContent":
        """
        Store the file content in the database.

        If the content is already stored, returns the reference to the existing
        content object. Otherwise stores it to the database and returns the newly
        created content object.

        :param session: The database session.
        :param hash: The hash of the file content, used as an unique ID.
        :param content: The file content as unicode string.
        :param meta: Optional metadata for the file content.
        :return: The file content object.
        """
        result = await session.execute(select(FileContent).where(FileContent.id == hash))
        fc = result.scalar_one_or_none()
        if fc is not None:
            if meta is not None:
                fc.meta = meta
            return fc

        fc = cls(id=hash, content=content, meta=meta or {})
        session.add(fc)

        return fc

    @classmethod
    async def delete_orphans(cls, session: AsyncSession):
        """
        Delete FileContent objects that are not referenced by any File object.

        :param session: The database session.
        """
        from core.db.models import File

        await session.execute(delete(FileContent).where(~FileContent.id.in_(select(distinct(File.content_id)))))
