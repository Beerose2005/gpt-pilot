import asyncio
import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from core.agents.base import BaseAgent
from core.agents.convo import AgentConvo
from core.agents.mixins import FileDiffMixin
from core.agents.response import AgentResponse, ResponseType
from core.config import CODE_MONKEY_AGENT_NAME, DESCRIBE_FILES_AGENT_NAME, IMPLEMENT_CHANGES_AGENT_NAME
from core.config.actions import CM_UPDATE_FILES
from core.db.models import File
from core.llm.convo import Convo
from core.llm.parser import JSONParser, OptionalCodeBlockParser
from core.log import get_logger

log = get_logger(__name__)


# Constant for indicating missing new line at the end of a file in a unified diff
NO_EOL = "\\ No newline at end of file"

# Regular expression pattern for matching hunk headers
PATCH_HEADER_PATTERN = re.compile(r"^@@ -(\d+),?(\d+)? \+(\d+),?(\d+)? @@")

# Maximum number of attempts to ask for review if it can't be parsed
MAX_REVIEW_RETRIES = 2

# Maximum number of code implementation attempts after which we accept the changes unconditionally
MAX_CODING_ATTEMPTS = 3


class Decision(str, Enum):
    APPLY = "apply"
    IGNORE = "ignore"
    REWORK = "rework"


class Hunk(BaseModel):
    number: int = Field(description="Index of the hunk in the diff. Starts from 1.")
    reason: str = Field(description="Reason for applying or ignoring this hunk, or for asking for it to be reworked.")
    decision: Decision = Field(description="Whether to apply this hunk, rework, or ignore it.")


class ReviewChanges(BaseModel):
    hunks: list[Hunk]
    review_notes: str = Field(description="Additional review notes (optional, can be empty).")


class FileDescription(BaseModel):
    summary: str = Field(
        description="Detailed description summarized what the file is about, and what the major classes, functions, elements or other functionality is implemented."
    )
    references: list[str] = Field(
        description="List of references the file imports or includes (only files local to the project), where each element specifies the project-relative path of the referenced file, including the file extension."
    )


def extract_code_blocks(content):
    # Use regex to find all <pythagoracode> blocks with file attribute and their content
    code_blocks = re.findall(r'<pythagoracode\s+file="(.*?)">(.*?)</pythagoracode>', content, re.DOTALL)
    # Convert matches into a list of dictionaries
    return [{"file_name": file_name, "file_content": file_content} for file_name, file_content in code_blocks]


class CodeMonkey(FileDiffMixin, BaseAgent):
    agent_type = "code-monkey"
    display_name = "Code Monkey"

    async def run(self) -> AgentResponse:
        if self.prev_response and self.prev_response.type == ResponseType.DESCRIBE_FILES:
            return await self.describe_files()
        else:
            data = await self.implement_changes()
            if not data:
                return AgentResponse.done(self)
            return await self.accept_changes(data["path"], data["old_content"], data["new_content"])

    async def implement_changes(self, data: Optional[dict] = None) -> dict:
        file_name = self.step["save_file"]["path"]

        current_file = await self.state_manager.get_file_by_path(file_name)
        file_content = current_file.content.content if current_file else ""

        if data is not None:
            attempt = data["attempt"] + 1
            feedback = data["feedback"]
            log.debug(f"Fixing file {file_name} after review feedback: {feedback} ({attempt}. attempt)")
            await self.ui.send_file_status(file_name, "reworking", source=self.ui_source)
        else:
            log.debug(f"Implementing file {file_name}")
            if data is None:
                await self.ui.send_file_status(
                    file_name, "updating" if file_content else "creating", source=self.ui_source
                )
            else:
                await self.ui.send_file_status(file_name, "reworking", source=self.ui_source)
            self.next_state.action = CM_UPDATE_FILES
            feedback = None

        iterations = self.current_state.iterations
        user_feedback = None
        user_feedback_qa = None

        if iterations:
            last_iteration = iterations[-1]
            instructions = last_iteration.get("description")
            user_feedback = last_iteration.get("user_feedback")
            user_feedback_qa = last_iteration.get("user_feedback_qa")
        else:
            instructions = self.current_state.current_task["instructions"]

        blocks = extract_code_blocks(instructions)
        response = None

        if blocks and self.state_manager.get_access_token():
            try:
                # Try Relace first
                block = next((item for item in blocks if item["file_name"] == file_name), None)
                if block:
                    llm = self.get_llm(IMPLEMENT_CHANGES_AGENT_NAME)
                    convo = Convo().user(
                        {
                            "initialCode": file_content,
                            "editSnippet": block["file_content"],
                        }
                    )
                    response = await llm(convo, temperature=0, parser=OptionalCodeBlockParser())
            except Exception:
                response = None

        # Fall back to OpenAI if Relace wasn't used or returned empty response
        if not response or response is None:
            llm = self.get_llm(CODE_MONKEY_AGENT_NAME)
            convo = AgentConvo(self).template(
                "implement_changes",
                file_name=file_name,
                file_content=file_content,
                instructions=instructions,
                user_feedback=user_feedback,
                user_feedback_qa=user_feedback_qa,
            )
            if feedback:
                convo.assistant(f"```\n{data['new_content']}\n```\n").template(
                    "review_feedback",
                    content=data["approved_content"],
                    original_content=file_content,
                    rework_feedback=feedback,
                )
            response = await llm(convo, temperature=0, parser=OptionalCodeBlockParser())

        return {
            "path": file_name,
            "old_content": file_content,
            "new_content": response,
        }

    async def describe_files(self) -> AgentResponse:
        tasks = []
        to_describe = {
            file.path: file.content.content
            for file in self.current_state.files
            if not file.content.meta.get("description")
        }

        for file in self.next_state.files:
            content = to_describe.get(file.path)
            if content is None:
                continue

            if content == "":
                file.content.meta = {
                    **file.content.meta,
                    "description": "Empty file",
                    "references": [],
                }
                continue

            tasks.append(self.describe_file(file, content))

        await asyncio.gather(*tasks)
        return AgentResponse.done(self)

    async def describe_file(self, file: File, content: str):
        """
        Describes a file by sending it to the LLM agent and then updating the file's metadata in the database.
        """
        llm = self.get_llm(DESCRIBE_FILES_AGENT_NAME)
        log.debug(f"Describing file {file.path}")
        convo = (
            AgentConvo(self)
            .template(
                "describe_file",
                path=file.path,
                content=content,
            )
            .require_schema(FileDescription)
        )
        llm_response: FileDescription = await llm(convo, parser=JSONParser(spec=FileDescription))

        file.content.meta = {
            **file.content.meta,
            "description": llm_response.summary,
            "references": llm_response.references,
        }

    # ------------------------------
    # CODE REVIEW
    # ------------------------------

    async def accept_changes(self, file_path: str, old_content: str, new_content: str) -> AgentResponse:
        await self.ui.send_file_status(file_path, "done", source=self.ui_source)

        n_new_lines, n_del_lines = self.get_line_changes(old_content, new_content)
        await self.ui.generate_diff(
            file_path, old_content, new_content, n_new_lines, n_del_lines, source=self.ui_source
        )

        await self.state_manager.save_file(file_path, new_content)
        self.step["save_file"]["content"] = new_content
        self.next_state.complete_step("save_file")

        input_required = self.state_manager.get_input_required(new_content, file_path)
        if input_required:
            return AgentResponse.input_required(
                self,
                [{"file": file_path, "line": line} for line in input_required],
            )
        else:
            return AgentResponse.done(self)
