import re
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from sqlalchemy import ForeignKey, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db.models import Base

if TYPE_CHECKING:
    from core.db.models import FileContent, ProjectState


class File(Base):
    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("project_state_id", "path"),)

    # ID and parent FKs
    id: Mapped[int] = mapped_column(primary_key=True)
    project_state_id: Mapped[UUID] = mapped_column(ForeignKey("project_states.id", ondelete="CASCADE"))
    content_id: Mapped[str] = mapped_column(ForeignKey("file_contents.id", ondelete="RESTRICT"))

    # Attributes
    path: Mapped[str] = mapped_column()

    # Relationships
    project_state: Mapped[Optional["ProjectState"]] = relationship(back_populates="files", lazy="raise")
    content: Mapped["FileContent"] = relationship(back_populates="files", lazy="selectin")

    def clone(self) -> "File":
        """
        Clone the file object, to be used in a new project state.

        The clone references the same file content object as the original.

        :return: The cloned file object.
        """
        return File(
            project_state=None,
            content_id=self.content_id,
            path=self.path,
        )

    @staticmethod
    async def get_referencing_files(session: "AsyncSession", project_state, file_path_to_search) -> list["File"]:
        results = await session.execute(select(File).where(File.project_state_id == project_state.id))
        all_files = results.scalars().all()

        file_to_search = None
        for file in all_files:
            if file.path == file_path_to_search:
                file_to_search = file
                all_files.remove(file)
                break

        if file_to_search is None:
            return []

        referencing_files = []
        target_file_name = file_path_to_search.split("/")[-1].split(".")[0]

        import_regex = re.compile(
            rf"import.*from\s+['\"](\.?/?(?:{re.escape(target_file_name)}|{re.escape('/api' + '/' + target_file_name)}))(?:['\"])[;]*"
        )

        # Extract function names from the target file
        function_names = set()
        for match in re.finditer(r"export\s+const\s+(\w+)\s*=", file_to_search.content.content):
            function_names.add(match.group(1))
        function_names_list = list(function_names)

        direct_function_call_regex = None
        if function_names_list:
            direct_function_call_regex = re.compile(rf"({'|'.join(function_names_list)})\(")

        for file in all_files:
            if import_regex.search(file.content.content):
                referencing_files.append(file)
            elif any(fn in file.content.content for fn in function_names_list):
                referencing_files.append(file)
            elif direct_function_call_regex and direct_function_call_regex.search(file.content.content):
                referencing_files.append(file)

        return referencing_files
