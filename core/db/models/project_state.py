from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Union
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, UniqueConstraint, and_, delete, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, load_only, mapped_column, relationship
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql import func

from core.config.actions import FE_START, PS_EPIC_COMPLETE
from core.db.models import Base, FileContent
from core.log import get_logger

if TYPE_CHECKING:
    from core.db.models import (
        Branch,
        ChatConvo,
        ExecLog,
        File,
        FileContent,
        KnowledgeBase,
        LLMRequest,
        Specification,
        UserInput,
    )

log = get_logger(__name__)


class TaskStatus:
    """Status of a task."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEWED = "reviewed"
    DOCUMENTED = "documented"
    DONE = "done"
    SKIPPED = "skipped"


class IterationStatus:
    """Status of an iteration."""

    HUNTING_FOR_BUG = "check_logs"
    AWAITING_LOGGING = "awaiting_logging"
    AWAITING_USER_TEST = "awaiting_user_test"
    AWAITING_BUG_FIX = "awaiting_bug_fix"
    AWAITING_BUG_REPRODUCTION = "awaiting_bug_reproduction"
    IMPLEMENT_SOLUTION = "implement_solution"
    FIND_SOLUTION = "find_solution"
    PROBLEM_SOLVER = "problem_solver"
    NEW_FEATURE_REQUESTED = "new_feature_requested"
    START_PAIR_PROGRAMMING = "start_pair_programming"
    DONE = "done"


class ProjectState(Base):
    __tablename__ = "project_states"
    __table_args__ = (
        UniqueConstraint("prev_state_id"),
        UniqueConstraint("branch_id", "step_index"),
        {"sqlite_autoincrement": True},
    )

    # ID and parent FKs
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    branch_id: Mapped[UUID] = mapped_column(ForeignKey("branches.id", ondelete="CASCADE"))
    prev_state_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("project_states.id", ondelete="CASCADE"))
    specification_id: Mapped[int] = mapped_column(ForeignKey("specifications.id"))
    knowledge_base_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"))

    # Attributes
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    step_index: Mapped[int] = mapped_column(default=1, server_default="1")
    epics: Mapped[list[dict]] = mapped_column(default=list)
    tasks: Mapped[list[dict]] = mapped_column(default=list)
    steps: Mapped[list[dict]] = mapped_column(default=list)
    iterations: Mapped[list[dict]] = mapped_column(default=list)
    relevant_files: Mapped[Optional[list[str]]] = mapped_column(default=None)
    modified_files: Mapped[dict] = mapped_column(default=dict)
    docs: Mapped[Optional[list[dict]]] = mapped_column(default=None)
    run_command: Mapped[Optional[str]] = mapped_column()
    action: Mapped[Optional[str]] = mapped_column()

    # Relationships
    branch: Mapped["Branch"] = relationship(back_populates="states", lazy="selectin")
    prev_state: Mapped[Optional["ProjectState"]] = relationship(
        back_populates="next_state",
        remote_side=[id],
        single_parent=True,
        lazy="raise",
        cascade="delete",
    )
    next_state: Mapped[Optional["ProjectState"]] = relationship(back_populates="prev_state", lazy="raise")
    files: Mapped[list["File"]] = relationship(
        back_populates="project_state",
        lazy="selectin",
        cascade="all,delete-orphan",
    )
    specification: Mapped["Specification"] = relationship(back_populates="project_states", lazy="selectin")
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="project_states", lazy="selectin")
    llm_requests: Mapped[list["LLMRequest"]] = relationship(back_populates="project_state", cascade="all", lazy="raise")
    user_inputs: Mapped[list["UserInput"]] = relationship(back_populates="project_state", cascade="all", lazy="raise")
    exec_logs: Mapped[list["ExecLog"]] = relationship(back_populates="project_state", cascade="all", lazy="raise")
    chat_convos: Mapped[list["ChatConvo"]] = relationship(
        back_populates="project_state", cascade="all,delete-orphan", lazy="raise"
    )

    @property
    def unfinished_steps(self) -> list[dict]:
        """
        Get the list of unfinished steps.

        :return: List of unfinished steps.
        """
        return [step for step in self.steps if not step.get("completed")]

    @property
    def current_step(self) -> Optional[dict]:
        """
        Get the current step.

        Current step is always the first step that's not finished yet.

        :return: The current step, or None if there are no more unfinished steps.
        """
        li = self.unfinished_steps
        return li[0] if li else None

    @property
    def unfinished_iterations(self) -> list[dict]:
        """
        Get the list of unfinished iterations.

        :return: List of unfinished iterations.
        """
        if not self.iterations:
            return []
        return [
            iteration for iteration in self.iterations if iteration.get("status") not in (None, IterationStatus.DONE)
        ]

    @property
    def current_iteration(self) -> Optional[dict]:
        """
        Get the current iteration.

        Current iteration is always the first iteration that's not finished yet.

        :return: The current iteration, or None if there are no unfinished iterations.
        """
        li = self.unfinished_iterations
        return li[0] if li else None

    @property
    def unfinished_tasks(self) -> list[dict]:
        """
        Get the list of unfinished tasks.

        :return: List of unfinished tasks.
        """
        if not self.tasks:
            return []
        return [task for task in self.tasks if task.get("status") != TaskStatus.DONE]

    @property
    def current_task(self) -> Optional[dict]:
        """
        Get the current task.

        Current task is always the first task that's not finished yet.

        :return: The current task, or None if there are no unfinished tasks.
        """
        li = self.unfinished_tasks
        return li[0] if li else None

    @property
    def unfinished_epics(self) -> list[dict]:
        """
        Get the list of unfinished epics.

        :return: List of unfinished epics.
        """
        return [epic for epic in self.epics if not epic.get("completed")]

    @property
    def current_epic(self) -> Optional[dict]:
        """
        Get the current epic.

        Current epic is always the first epic that's not finished yet.

        :return: The current epic, or None if there are no unfinished epics.
        """
        li = self.unfinished_epics
        return li[0] if li else None

    @property
    def relevant_file_objects(self):
        """
        Get the relevant files with their content.

        :return: List of tuples with file path and content.
        """
        relevant_files = self.relevant_files or []
        modified_files = self.modified_files or {}

        all_files = set(relevant_files + list(modified_files.keys()))
        return [file for file in self.files if file.path in all_files]

    @staticmethod
    def create_initial_state(branch: "Branch") -> "ProjectState":
        """
        Create the initial project state for a new branch.

        This does *not* commit the new state to the database.

        No checks are made to ensure that the branch does not
        already have a state.

        :param branch: The branch to create the state for.
        :return: The new ProjectState object.
        """
        from core.db.models import KnowledgeBase, Specification

        return ProjectState(
            branch=branch,
            specification=Specification(),
            knowledge_base=KnowledgeBase(),
            step_index=1,
            action="Initial project state",
        )

    @staticmethod
    async def get_project_states(
        session: "AsyncSession",
        project_id: Optional[UUID] = None,
        branch_id: Optional[UUID] = None,
    ) -> list["ProjectState"]:
        from core.db.models import Branch, ProjectState

        branch = None
        limit = 100

        if branch_id:
            branch = await session.execute(select(Branch).where(Branch.id == branch_id))
            branch = branch.scalar_one_or_none()
        elif project_id:
            branch = await session.execute(select(Branch).where(Branch.project_id == project_id))
            branch = branch.scalar_one_or_none()

        if branch:
            query = (
                select(ProjectState)
                .where(ProjectState.branch_id == branch.id)
                .order_by(ProjectState.step_index.desc())  # Get the latest 100 states
                .limit(limit)
            )

            project_states_result = await session.execute(query)
            project_states = project_states_result.scalars().all()
            return sorted(project_states, key=lambda x: x.step_index)

        return []

    async def create_next_state(self) -> "ProjectState":
        """
        Create the next project state for the branch.

        This does NOT insert the new state and the associated objects (spec,
        files, ...) to the database.

        :param session: The SQLAlchemy session.
        :return: The new ProjectState object.
        """
        if not self.id:
            raise ValueError("Cannot create next state for unsaved state.")

        if "next_state" in self.__dict__:
            raise ValueError(f"Next state already exists for state with id={self.id}.")

        new_state = ProjectState(
            branch=self.branch,
            prev_state=self,
            step_index=self.step_index + 1,
            specification=self.specification,
            epics=deepcopy(self.epics),
            tasks=deepcopy(self.tasks),
            steps=deepcopy(self.steps),
            iterations=deepcopy(self.iterations),
            knowledge_base=self.knowledge_base,
            files=[],
            relevant_files=deepcopy(self.relevant_files),
            modified_files=deepcopy(self.modified_files),
            docs=deepcopy(self.docs),
            run_command=self.run_command,
        )

        session: AsyncSession = inspect(self).async_session
        session.add(new_state)

        # NOTE: we only need the await here because of the tests, in live, the
        # load_project() and commit() methods on StateManager make sure that
        # the the files are eagerly loaded.
        for file in await self.awaitable_attrs.files:
            clone = file.clone()
            new_state.files.append(clone)
            # Load content for the clone using the same content_id
            result = await session.execute(select(FileContent).where(FileContent.id == file.content_id))
            clone.content = result.scalar_one_or_none()

        return new_state

    def complete_step(self, step_type: str):
        if not self.unfinished_steps:
            raise ValueError("There are no unfinished steps to complete")
        if "next_state" in self.__dict__:
            raise ValueError("Current state is read-only (already has a next state).")

        log.debug(f"Completing step {self.unfinished_steps[0]['type']}")
        self.get_steps_of_type(step_type)[0]["completed"] = True
        flag_modified(self, "steps")

    def complete_task(self):
        if not self.unfinished_tasks:
            raise ValueError("There are no unfinished tasks to complete")
        if "next_state" in self.__dict__:
            raise ValueError("Current state is read-only (already has a next state).")

        log.debug(f"Completing task {self.unfinished_tasks[0]['description']}")
        self.set_current_task_status(TaskStatus.DONE)
        self.steps = []
        self.iterations = []
        self.relevant_files = None
        self.modified_files = {}
        self.docs = None
        flag_modified(self, "tasks")

        if not self.unfinished_tasks and self.unfinished_epics:
            self.complete_epic()

    def complete_epic(self):
        if not self.unfinished_epics:
            raise ValueError("There are no unfinished epics to complete")
        if "next_state" in self.__dict__:
            raise ValueError("Current state is read-only (already has a next state).")

        log.debug(f"Completing epic {self.unfinished_epics[0]['name']}")
        self.unfinished_epics[0]["completed"] = True
        self.tasks = []
        flag_modified(self, "epics")
        if len(self.unfinished_epics) > 0:
            self.next_state.action = PS_EPIC_COMPLETE.format(self.unfinished_epics[0]["name"])

    def complete_iteration(self):
        if not self.unfinished_iterations:
            raise ValueError("There are no unfinished iterations to complete")
        if "next_state" in self.__dict__:
            raise ValueError("Current state is read-only (already has a next state).")

        log.debug(f"Completing iteration {self.unfinished_iterations[0]}")
        self.unfinished_iterations[0]["status"] = IterationStatus.DONE
        self.relevant_files = None
        self.modified_files = {}
        self.flag_iterations_as_modified()

    def flag_iterations_as_modified(self):
        """
        Flag the iterations field as having been modified

        Used by Agents that perform modifications within the mutable iterations field,
        to tell the database that it was modified and should get saved (as SQLalchemy
        can't detect changes in mutable fields by itself).
        """
        flag_modified(self, "iterations")

    def flag_tasks_as_modified(self):
        """
        Flag the tasks field as having been modified

        Used by Agents that perform modifications within the mutable tasks field,
        to tell the database that it was modified and should get saved (as SQLalchemy
        can't detect changes in mutable fields by itself).
        """
        flag_modified(self, "tasks")

    def flag_epics_as_modified(self):
        """
        Flag the epic field as having been modified

        Used by Agents that perform modifications within the mutable epics field,
        to tell the database that it was modified and should get saved (as SQLalchemy
        can't detect changes in mutable fields by itself).
        """
        flag_modified(self, "epics")

    def flag_knowledge_base_as_modified(self):
        """
        Flag the knowledge base fields as having been modified

        Used by Agents that perform modifications within the mutable knowledge base fields,
        to tell the database that it was modified and should get saved (as SQLalchemy
        can't detect changes in mutable fields by itself).

        This creates a new knowledge base instance to maintain immutability of previous states,
        similar to how specification modifications are handled.
        """
        # Create a new knowledge base instance with the current data
        self.knowledge_base = self.knowledge_base.clone()

    def set_current_task_status(self, status: str):
        """
        Set the status of the current task.

        :param status: The new status.
        """
        if not self.current_task:
            raise ValueError("There is no current task to set status for")
        if "next_state" in self.__dict__:
            raise ValueError("Current state is read-only (already has a next state).")

        self.current_task["status"] = status
        self.flag_tasks_as_modified()

    def get_file_by_path(self, path: str) -> Optional["File"]:
        """
        Get a file from the current project state, by the file path.

        :param path: The file path.
        :return: The file object, or None if not found.
        """
        for file in self.files:
            if file.path == path:
                return file

        return None

    def get_file_content_by_path(self, path: str) -> Union[FileContent, str]:
        """
        Get a file from the current project state, by the file path.

        :param path: The file path.
        :return: The file object, or None if not found.
        """
        file = self.get_file_by_path(path)

        return file.content.content if file else ""

    def save_file(self, path: str, content: "FileContent", external: bool = False) -> "File":
        """
        Save a file to the project state.

        This either creates a new file pointing at the given content,
        or updates the content of an existing file. This method
        doesn't actually commit the file to the database, just attaches
        it to the project state.

        If the file was created by Pythagora (not externally by user or template import),
        mark it as relevant for the current task.

        :param path: The file path.
        :param content: The file content.
        :param external: Whether the file was added externally (e.g. by a user).
        :return: The (unsaved) file object.
        """
        from core.db.models import File

        if "next_state" in self.__dict__:
            raise ValueError("Current state is read-only (already has a next state).")

        file = self.get_file_by_path(path)
        if file:
            original_content = file.content.content
            file.content = content
        else:
            original_content = ""
            file = File(path=path, content=content)
            self.files.append(file)

        if path not in self.modified_files and not external:
            self.modified_files[path] = original_content

        self.relevant_files = self.relevant_files or []
        if path not in self.relevant_files:
            self.relevant_files.append(path)

        return file

    async def delete_after(self):
        """
        Delete all states in the branch after this one, along with related data.

        This includes:
        - ProjectState records after this one
        - Related UserInput records (including those for the current state)
        - Related File records
        - Orphaned FileContent records
        - Orphaned KnowledgeBase records
        - Orphaned Specification records
        """
        from core.db.models import FileContent, KnowledgeBase, Specification, UserInput

        session: AsyncSession = inspect(self).async_session

        log.debug(f"Deleting all project states in branch {self.branch_id} after {self.id}")

        # Get all project states to be deleted
        states_to_delete = await session.execute(
            select(ProjectState).where(
                ProjectState.branch_id == self.branch_id,
                ProjectState.step_index > self.step_index,
            )
        )
        states_to_delete = states_to_delete.scalars().all()
        state_ids = [state.id for state in states_to_delete]

        # Delete user inputs for the current state
        await session.execute(delete(UserInput).where(UserInput.project_state_id == self.id))

        if state_ids:
            # Delete related user inputs for states to be deleted
            await session.execute(delete(UserInput).where(UserInput.project_state_id.in_(state_ids)))

            # Delete project states
            await session.execute(delete(ProjectState).where(ProjectState.id.in_(state_ids)))

        # Clean up orphaned records
        await FileContent.delete_orphans(session)
        await UserInput.delete_orphans(session)
        await KnowledgeBase.delete_orphans(session)
        await Specification.delete_orphans(session)

    def get_last_iteration_steps(self) -> list:
        """
        Get the steps of the last iteration.

        :return: A list of steps.
        """
        return [s for s in self.steps if s.get("iteration_index") == len(self.iterations)] or self.steps

    def get_source_index(self, source: str) -> int:
        """
        Get the index of the source which can be one of: 'app', 'feature', 'troubleshooting', 'review'. For example,
        for feature return value would be number of current feature.

        :param source: The source to search for.
        :return: The index of the source.
        """
        if source in ["app", "feature"]:
            return len([epic for epic in self.epics if epic.get("source") == source])
        elif source == "troubleshooting":
            return len(self.iterations)
        elif source == "review":
            steps = self.get_last_iteration_steps()
            return len([step for step in steps if step.get("type") == "review_task"])

        return 1

    def get_steps_of_type(self, step_type: str) -> [dict]:
        """
        Get list of unfinished steps with specific type.

        :return: List of steps, or empty list if there are no unfinished steps of that type.
        """
        li = self.unfinished_steps
        return [step for step in li if step.get("type") == step_type] if li else []

    def has_frontend(self) -> bool:
        """
        Check if there is a frontend epic in the project state.

        :return: True if there is a frontend epic, False otherwise.
        """
        return self.epics and any(epic.get("source") == "frontend" for epic in self.epics)

    # function that checks whether old project or new project is currently in frontend stage
    def working_on_frontend(self) -> bool:
        return self.has_frontend() and len(self.epics) == 1

    def is_feature(self) -> bool:
        """
        Check if the current epic is a feature.

        :return: True if the current epic is a feature, False otherwise.
        """
        return self.epics and self.current_epic and self.current_epic.get("source") == "feature"

    @staticmethod
    async def get_state_for_redo_task(session: AsyncSession, project_state: "ProjectState") -> Optional["ProjectState"]:
        states_result = await session.execute(
            select(ProjectState).where(
                and_(
                    ProjectState.step_index <= project_state.step_index,
                    ProjectState.branch_id == project_state.branch_id,
                )
            )
        )

        result = states_result.scalars().all()

        result = sorted(result, key=lambda x: x.step_index, reverse=True)
        for state in result:
            if state.tasks:
                for task in state.tasks:
                    if task.get("id") == project_state.current_task.get("id") and task.get("instructions") is None:
                        if task.get("status") == TaskStatus.TODO:
                            return state

        return None

    @staticmethod
    async def get_by_id(session: "AsyncSession", state_id: UUID) -> Optional["ProjectState"]:
        """
        Retrieve a project state by its ID.

        :param session: The SQLAlchemy async session.
        :param state_id: The UUID of the project state to retrieve.
        :return: The ProjectState object if found, None otherwise.
        """
        if not state_id:
            return None

        query = select(ProjectState).where(ProjectState.id == state_id)
        result = await session.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_all_epics_and_tasks(session: "AsyncSession", branch_id: UUID) -> list:
        epics_and_tasks = []

        try:
            query = (
                select(ProjectState)
                .options(load_only(ProjectState.id, ProjectState.epics, ProjectState.tasks))
                .where(and_(ProjectState.branch_id == branch_id, ProjectState.action.isnot(None)))
            )

            result = await session.execute(query)
            project_states = result.scalars().all()

            def has_epic(epic_type: str):
                return any(epic1.get("source", "") == epic_type for epic1 in epics_and_tasks)

            def find_epic_by_id(epic_id: str, sub_epic_id: str):
                return next(
                    (
                        epic
                        for epic in epics_and_tasks
                        if epic.get("id", "") == epic_id and epic.get("sub_epic_id", "") == sub_epic_id
                    ),
                    None,
                )

            def find_task_in_epic(task_id: str, epic):
                if not epic:
                    return None
                return next((task for task in epic.get("tasks", []) if task.get("id", "") == task_id), None)

            for state in project_states:
                epics, tasks = state.epics, state.tasks
                epic = epics[-1]

                if epics[-1] in ["spec_writer", "frontend"]:
                    for epic in state.epics:
                        if epic["source"] == "spec_writer" and not has_epic("spec_writer"):
                            epics_and_tasks.insert(0, {"source": "spec_writer", "tasks": []})

                        if epic["source"] == "frontend" and not has_epic("frontend"):
                            epics_and_tasks.insert(1, {"source": "frontend", "tasks": []})

                else:
                    for sub_epic in epic.get("sub_epics", []):
                        if not find_epic_by_id(epic["id"], sub_epic["id"]):
                            epics_and_tasks.append(
                                {
                                    "id": epic["id"],
                                    "sub_epic_id": sub_epic["id"],
                                    "source": epic["source"],
                                    "description": sub_epic.get("description", ""),
                                    "tasks": [],
                                }
                            )

                        for task in tasks:
                            epic_in_list = find_epic_by_id(epic["id"], task.get("sub_epic_id"))
                            if not epic_in_list:
                                continue
                            task_in_epic_list = find_task_in_epic(task["id"], epic_in_list)
                            if not task_in_epic_list:
                                epic_in_list["tasks"].append(
                                    {
                                        "id": task.get("id"),
                                        "status": task.get("status"),
                                        "description": task.get("description"),
                                    }
                                )
                            else:
                                # Update the status of the task if it already exists
                                task_in_epic_list["status"] = task.get("status")

        except Exception as e:
            log.error(f"Error while getting epics and tasks: {e}")
            return []

        return epics_and_tasks

    @staticmethod
    async def get_project_states_in_between(
        session: "AsyncSession", branch_id: UUID, start_id: UUID, end_id: UUID, limit: Optional[int] = 100
    ):
        query = select(ProjectState).where(
            and_(
                ProjectState.branch_id == branch_id,
                ProjectState.id == start_id,
            )
        )
        result = await session.execute(query)
        start_state = result.scalars().one_or_none()

        query = select(ProjectState).where(
            and_(
                ProjectState.branch_id == branch_id,
                ProjectState.id == end_id,
            )
        )
        result = await session.execute(query)
        end_state = result.scalars().one_or_none()

        if not start_state or not end_state:
            log.error(f"Could not find states with IDs {start_id} and {end_id} in branch {branch_id}")
            return []

        query = (
            select(ProjectState)
            .where(
                and_(
                    ProjectState.branch_id == branch_id,
                    ProjectState.step_index >= start_state.step_index,
                    ProjectState.step_index <= end_state.step_index,
                )
            )
            .order_by(ProjectState.step_index.desc())
        )

        if limit:
            query = query.limit(limit)

        result = await session.execute(query)
        states = result.scalars().all()

        # Since we always order by step_index desc, we need to reverse to get chronological order
        return list(reversed(states))

    @staticmethod
    async def get_task_conversation_project_states(
        session: "AsyncSession",
        branch_id: UUID,
        task_id: UUID,
        first_last_only: bool = False,
        limit: Optional[int] = 25,
    ) -> Optional[list["ProjectState"]]:
        """
        Retrieve the conversation for the task in the project state.

        :param session: The SQLAlchemy async session.
        :param branch_id: The UUID of the branch.
        :param task_id: The UUID of the task.
        :param first_last_only: If True, return only first and last states.
        :param limit: Maximum number of states to return (default 25).
        :return: List of conversation messages if found, None otherwise.
        """
        log.debug(
            f"Getting task conversation project states for task {task_id} in branch {branch_id} with first_last_only {first_last_only} and limit {limit}"
        )
        # First, we need to find the start and end step indices
        # Use a more efficient query that only loads necessary fields
        query = (
            select(ProjectState)
            .options(load_only(ProjectState.id, ProjectState.step_index, ProjectState.tasks, ProjectState.action))
            .where(
                and_(
                    ProjectState.branch_id == branch_id,
                    or_(ProjectState.action.like("%Task #%"), ProjectState.action.like("%Create a development plan%")),
                )
            )
            .order_by(ProjectState.step_index)
        )

        result = await session.execute(query)
        states = result.scalars().all()

        log.debug(f"Found {len(states)} states with custom action")

        start_step_index = None
        end_step_index = None

        # for the FIRST task, it is todo in the same state as Create a development plan, while other tasks are "Task #N start" (action)

        # this is done solely to be able to reload to the first task, due to the fact that we need the same project_state_id for the send_back_logs
        # for the first task, we need to start from the FIRST state that has that task in TODO status
        # for all other tasks, we need to start from LAST state that has that task in TODO status
        for state in states:
            for task in state.tasks:
                if UUID(task["id"]) == task_id and task.get("status", "") == TaskStatus.TODO:
                    if UUID(task["id"]) == UUID(state.tasks[0]["id"]):
                        # First task: set start only once (first occurrence)
                        if start_step_index is None:
                            start_step_index = state.step_index
                    else:
                        # Other tasks: update start every time (last occurrence)
                        start_step_index = state.step_index

                if UUID(task["id"]) == task_id and task.get("status", "") in [
                    TaskStatus.SKIPPED,
                    TaskStatus.DOCUMENTED,
                    TaskStatus.REVIEWED,
                    TaskStatus.DONE,
                ]:
                    end_step_index = state.step_index

        if start_step_index is None:
            return []

        # Now build the optimized query based on what we need
        if first_last_only:
            # For first_last_only, we only need the first and last states
            # Get first state
            first_query = (
                select(ProjectState)
                .where(
                    and_(
                        ProjectState.branch_id == branch_id,
                        ProjectState.step_index >= start_step_index,
                        ProjectState.step_index < end_step_index if end_step_index else True,
                    )
                )
                .order_by(ProjectState.step_index.asc())
                .limit(1)
            )

            # Get last state (excluding the uncommitted one)
            last_query = (
                select(ProjectState)
                .where(
                    and_(
                        ProjectState.branch_id == branch_id,
                        ProjectState.step_index >= start_step_index,
                        ProjectState.step_index < end_step_index if end_step_index else True,
                    )
                )
                .order_by(ProjectState.step_index.desc())
                .limit(2)
            )  # Get last 2 to exclude uncommitted

            first_result = await session.execute(first_query)
            last_result = await session.execute(last_query)

            first_state = first_result.scalars().first()
            last_states = last_result.scalars().all()

            # Remove the last state (uncommitted) and get the actual last
            if len(last_states) > 1:
                last_state = last_states[1]  # Second to last is the actual last committed
            else:
                last_state = first_state  # Only one state

            if first_state and last_state and first_state.id != last_state.id:
                return [first_state, last_state]
            elif first_state:
                return [first_state]
            else:
                return []

        else:
            # For regular queries, apply limit at the database level
            query = (
                select(ProjectState)
                .where(
                    and_(
                        ProjectState.branch_id == branch_id,
                        ProjectState.step_index >= start_step_index,
                        ProjectState.step_index < end_step_index if end_step_index else True,
                    )
                )
                .order_by(ProjectState.step_index.asc())
            )

            if limit:
                # Apply limit + 1 to account for removing the last uncommitted state
                query = query.limit(limit + 1)

            result = await session.execute(query)
            results = result.scalars().all()

            log.debug(f"Found {len(results)} states with custom action")
            # Remove the last state from the list because that state is not yet committed in the database!
            if results:
                results = results[:-1]

            return results

    @staticmethod
    async def get_fe_states(
        session: "AsyncSession", branch_id: UUID, limit: Optional[int] = None
    ) -> Optional["ProjectState"]:
        query = select(ProjectState).where(
            and_(
                ProjectState.branch_id == branch_id,
                ProjectState.action == FE_START,
            )
        )
        result = await session.execute(query)
        fe_start = result.scalars().one_or_none()

        if not fe_start:
            return []

        query = (
            select(ProjectState)
            .where(
                and_(
                    ProjectState.branch_id == branch_id,
                    ProjectState.step_index >= fe_start.step_index,
                    ProjectState.action.like("%Frontend%"),
                )
            )
            .order_by(ProjectState.step_index.desc())
            .limit(1)
        )
        result = await session.execute(query)
        fe_end = result.scalars().one_or_none()

        query = (
            select(ProjectState)
            .where(
                and_(
                    ProjectState.branch_id == branch_id,
                    ProjectState.step_index >= fe_start.step_index,
                    ProjectState.step_index <= fe_end.step_index,
                )
            )
            .order_by(ProjectState.step_index.desc())
        )

        if limit:
            query = query.limit(limit)

        results = await session.execute(query)
        states = results.scalars().all()

        # Since we ordered by step_index desc and limited, we need to reverse to get chronological order
        return list(reversed(states))

    @staticmethod
    def get_epic_task_number(state, current_task) -> (int, int):
        epic_num = -1
        task_num = -1

        for task in state.tasks:
            epic_n = task.get("sub_epic_id", 1) + 2
            if epic_n != epic_num:
                epic_num = epic_n
                task_num = 1

            if current_task["id"] == task["id"]:
                return epic_num, task_num

            task_num += 1

        return epic_num, task_num

    @staticmethod
    async def get_be_back_logs(session: "AsyncSession", branch_id: UUID) -> (list[dict], dict, list["ProjectState"]):
        """
        For each FINISHED task in the branch, find all project states where the task status changes. Additionally, the last task that will be returned is the one that is currently being worked on.
        Returns data formatted for the UI + the project states for history convo.

        :param session: The SQLAlchemy async session.
        :param branch_id: The UUID of the branch.
        :return: List of dicts with UI-friendly task conversation format.
        """
        query = select(ProjectState).where(
            and_(
                ProjectState.branch_id == branch_id,
                or_(ProjectState.action.like("%Task #%"), ProjectState.action.like("%Create a development plan%")),
            )
        )
        result = await session.execute(query)
        states = result.scalars().all()

        log.debug(f"Found {len(states)} states in branch")

        if not states:
            query = select(ProjectState).where(ProjectState.branch_id == branch_id)
            result = await session.execute(query)
            states = result.scalars().all()

        task_histories = []

        def find_task_history(task_id):
            for th in task_histories:
                if th["task_id"] == task_id:
                    return th
            return None

        for state in states:
            for task in state.tasks or []:
                task_id = task.get("id")
                if not task_id:
                    continue

                th = find_task_history(task_id)
                if not th:
                    th = {
                        "task_id": task_id,
                        "title": task.get("description"),
                        "labels": [],
                        "status": task["status"],
                        "start_id": state.id,
                        "project_state_id": state.id,
                        "end_id": state.id,
                    }
                    task_histories.append(th)

                if task.get("status") == TaskStatus.TODO:
                    th["status"] = TaskStatus.TODO
                    th["start_id"] = state.id
                    th["project_state_id"] = state.id
                    th["end_id"] = state.id

                elif task.get("status") != th["status"]:
                    th["status"] = task.get("status")
                    th["end_id"] = state.id

                epic_index, task_index = ProjectState.get_epic_task_number(state, task)
                th["labels"] = [
                    f"E{str(epic_index)} / T{task_index}",
                    "Backend",
                    "Working"
                    if task.get("status") in [TaskStatus.TODO, TaskStatus.IN_PROGRESS]
                    else "Skipped"
                    if task.get("status") == TaskStatus.SKIPPED
                    else "Done",
                ]

        last_task = {}

        # todo/in_progress can override done
        # done can override todo/in_progress
        # todo/in_progress can not override todo/in_progress

        for th in task_histories:
            if not last_task:
                last_task = th

            # if we have multiple tasks being Worked on (todo state) in a row, then we take the first one
            # if we see a Done task, we take that one
            if not (
                last_task["status"] in [TaskStatus.TODO, TaskStatus.IN_PROGRESS]
                and th["status"] in [TaskStatus.TODO, TaskStatus.IN_PROGRESS]
            ):
                last_task = th

        if task_histories and last_task:
            task_histories = task_histories[: task_histories.index(last_task) + 1]

        if last_task:
            project_states = await ProjectState.get_task_conversation_project_states(
                session, branch_id, UUID(last_task["task_id"])
            )
            if project_states:
                last_task["start_id"] = project_states[0].id
                last_task["project_state_id"] = project_states[0].id
                last_task["end_id"] = project_states[-1].id
        return task_histories, last_task
