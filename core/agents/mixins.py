import asyncio
import json
from typing import List, Optional

from pydantic import BaseModel, Field

from core.agents.convo import AgentConvo
from core.agents.response import AgentResponse
from core.cli.helpers import get_line_changes
from core.config import GET_RELEVANT_FILES_AGENT_NAME, TASK_BREAKDOWN_AGENT_NAME, TROUBLESHOOTER_BUG_REPORT
from core.config.actions import MIX_BREAKDOWN_CHAT_PROMPT
from core.config.constants import CONVO_ITERATIONS_LIMIT
from core.config.magic_words import ALWAYS_RELEVANT_FILES
from core.llm.parser import JSONParser
from core.log import get_logger
from core.ui.base import ProjectStage

log = get_logger(__name__)


class RelevantFiles(BaseModel):
    relevant_files: Optional[List[str]] = Field(
        description="List of files you want to add to the list of relevant files."
    )


class Test(BaseModel):
    title: str = Field(description="Very short title of the test.")
    action: str = Field(description="More detailed description of what actions have to be taken to test the app.")
    result: str = Field(description="Expected result that verifies successful test.")


class TestSteps(BaseModel):
    steps: List[Test]


class ChatWithBreakdownMixin:
    """
    Provides a method to chat with the user and provide a breakdown of the conversation.
    """

    async def chat_with_breakdown(self, convo: AgentConvo, breakdown: str) -> AgentConvo:
        """
        Chat with the user and provide a breakdown of the conversation.

        :param convo: The conversation object.
        :param breakdown: The breakdown of the conversation.
        :return: The breakdown.
        """

        llm = self.get_llm(TASK_BREAKDOWN_AGENT_NAME, stream_output=True)
        while True:
            await self.ui.send_project_stage(
                {
                    "stage": ProjectStage.BREAKDOWN_CHAT,
                    "agent": self.agent_type,
                }
            )

            if self.state_manager.auto_confirm_breakdown:
                break

            chat = await self.ask_question(
                MIX_BREAKDOWN_CHAT_PROMPT,
                buttons={"yes": "Yes, looks good!"},
                default="yes",
                verbose=False,
            )
            if chat.button == "yes":
                break

            if len(convo.messages) > CONVO_ITERATIONS_LIMIT:
                convo.slice(3, CONVO_ITERATIONS_LIMIT)

            convo.user(chat.text)
            breakdown: str = await llm(convo)
            convo.assistant(breakdown)

        return breakdown


class IterationPromptMixin:
    """
    Provides a method to find a solution to a problem based on user feedback.

    Used by ProblemSolver and Troubleshooter agents.
    """

    async def find_solution(
        self,
        user_feedback: str,
        *,
        user_feedback_qa: Optional[list[str]] = None,
        next_solution_to_try: Optional[str] = None,
        bug_hunting_cycles: Optional[dict] = None,
    ) -> str:
        """
        Generate a new solution for the problem the user reported.

        :param user_feedback: User feedback about the problem.
        :param user_feedback_qa: Additional q/a about the problem provided by the user (optional).
        :param next_solution_to_try: Hint from ProblemSolver on which solution to try (optional).
        :param bug_hunting_cycles: Data about logs that need to be added to the code (optional).
        :return: The generated solution to the problem.
        """
        llm = self.get_llm(TROUBLESHOOTER_BUG_REPORT, stream_output=True)
        convo = AgentConvo(self).template(
            "iteration",
            user_feedback=user_feedback,
            user_feedback_qa=user_feedback_qa,
            next_solution_to_try=next_solution_to_try,
            bug_hunting_cycles=bug_hunting_cycles,
            test_instructions=json.loads(self.current_state.current_task.get("test_instructions", "[]")),
        )
        llm_solution: str = await llm(convo)

        llm_solution = await self.chat_with_breakdown(convo, llm_solution)

        return llm_solution


class RelevantFilesMixin:
    """
    Asynchronously retrieves relevant files for the current task by separating front-end and back-end files, and processing them in parallel.

    This method initiates two asynchronous tasks to fetch relevant files for the front-end (client) and back-end (server) respectively.
    It then combines the results, filters out any non-existing files, and updates the current and next state with the relevant files.
    """

    async def get_relevant_files_parallel(
        self, user_feedback: Optional[str] = None, solution_description: Optional[str] = None
    ) -> AgentResponse:
        tasks = [
            self.get_relevant_files(
                user_feedback=user_feedback, solution_description=solution_description, dir_type="client"
            ),
            self.get_relevant_files(
                user_feedback=user_feedback, solution_description=solution_description, dir_type="server"
            ),
        ]

        responses = await asyncio.gather(*tasks)

        relevant_files = [item for sublist in responses for item in sublist]

        existing_files = {file.path for file in self.current_state.files}
        relevant_files = [path for path in relevant_files if path in existing_files]
        self.current_state.relevant_files = relevant_files
        self.next_state.relevant_files = relevant_files

        return AgentResponse.done(self)

    async def get_relevant_files(
        self,
        user_feedback: Optional[str] = None,
        solution_description: Optional[str] = None,
        dir_type: Optional[str] = None,
    ) -> list[str]:
        log.debug(
            "Getting relevant files for the current task for: " + ("frontend" if dir_type == "client" else "backend")
        )
        relevant_files = set()
        llm = self.get_llm(GET_RELEVANT_FILES_AGENT_NAME)
        convo = (
            AgentConvo(self)
            .template(
                "filter_files",
                user_feedback=user_feedback,
                solution_description=solution_description,
                relevant_files=relevant_files,
                dir_type=dir_type,
            )
            .require_schema(RelevantFiles)
        )
        llm_response: RelevantFiles = await llm(convo, parser=JSONParser(RelevantFiles), temperature=0)
        existing_files = {file.path for file in self.current_state.files}
        if not llm_response.relevant_files:
            return []
        paths_list = [path for path in llm_response.relevant_files if path in existing_files]

        try:
            for file_path in ALWAYS_RELEVANT_FILES:
                if file_path not in paths_list and file_path in existing_files:
                    paths_list.append(file_path)
        except Exception as e:
            log.error(f"Error while getting most important files: {e}")

        return paths_list


class FileDiffMixin:
    """
    Provides a method to generate a diff between two files.
    """

    def get_line_changes(self, old_content: str, new_content: str) -> tuple[int, int]:
        """
        Get the number of added and deleted lines between two files.

        This uses Python difflib to produce a unified diff, then counts
        the number of added and deleted lines.

        :param old_content: old file content
        :param new_content: new file content
        :return: a tuple (n_new_lines, n_del_lines)
        """

        return get_line_changes(old_content, new_content)
