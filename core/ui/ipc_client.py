import asyncio
import json
from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, ValidationError

from core.config import LocalIPCConfig
from core.log import get_logger
from core.ui.base import UIBase, UIClosedError, UISource, UserInput, UserInterruptError

VSCODE_EXTENSION_HOST = "localhost"
VSCODE_EXTENSION_PORT = 8125
MESSAGE_SIZE_LIMIT = 512 * 1024

log = get_logger(__name__)


# TODO: unify these (and corresponding changes in the extension) before release
# Also clean up (remove) double JSON encoding in some of the messages
class MessageType(str, Enum):
    EXIT = "exit"
    STREAM = "stream"
    VERBOSE = "verbose"
    RESPONSE = "response"
    USER_INPUT_REQUEST = "user_input_request"
    INFO = "info"
    STAGE = "stage"
    PROGRESS = "progress"
    DEBUGGING_LOGS = "debugging_logs"
    RUN_COMMAND = "run_command"
    APP_LINK = "appLink"
    OPEN_FILE = "openFile"
    PROJECT_STATS = "projectStats"
    KEY_EXPIRED = "keyExpired"
    LOADING_FINISHED = "loadingFinished"
    PROJECT_DESCRIPTION = "projectDescription"
    FEATURES_LIST = "featuresList"
    IMPORT_PROJECT = "importProject"
    APP_FINISHED = "appFinished"
    FEATURE_FINISHED = "featureFinished"
    GENERATE_DIFF = "generateDiff"
    CLOSE_DIFF = "closeDiff"
    FILE_STATUS = "fileStatus"
    BUG_HUNTER_STATUS = "bugHunterStatus"
    EPICS_AND_TASKS = "epicsAndTasks"
    CHAT_MESSAGE = "chatMessage"
    START_CHAT = "startChat"
    GET_CHAT_HISTORY = "getChatHistory"
    PROJECT_INFO = "projectInfo"
    KNOWLEDGE_BASE = "getKnowledgeBase"
    PROJECT_SPECS = "getProjectSpecs"
    TASK_CONVO = "getTaskConvo"
    EDIT_SPECS = "editSpecs"
    FILE_DIFF = "getFileDiff"
    TASK_CURRENT_STATUS = "getCurrentTaskStatus"
    TASK_ADD_NEW = "addNewTask"
    TASK_START_OTHER = "startOtherTask"
    CHAT_MESSAGE_RESPONSE = "chatMessageResponse"
    MODIFIED_FILES = "modifiedFiles"
    IMPORTANT_STREAM = "importantStream"
    BREAKDOWN_STREAM = "breakdownStream"
    TEST_INSTRUCTIONS = "testInstructions"
    KNOWLEDGE_BASE_UPDATE = "updatedKnowledgeBase"
    STOP_APP = "stopApp"
    TOKEN_EXPIRED = "tokenExpired"
    USER_INPUT_HISTORY = "userInputHistory"
    BACK_LOGS = "backLogs"
    LOAD_FRONT_LOGS = "loadFrontLogs"
    FRONT_LOGS_HEADERS = "frontLogsHeaders"
    CLEAR_MAIN_LOGS = "clearMainLogs"
    FATAL_ERROR = "fatalError"


class Message(BaseModel):
    """
    Message structure for IPC communication with the VSCode extension.

    Attributes:
    * `type`: Message type (always "response" for VSC server responses)
    * `category`: Message category (eg. "agent:product-owner"), optional
    * `content`: Message content (eg. "Hello, how are you?"), optional
    * `extra_info`: Additional information (eg. "This is a hint"), optional
    * `placeholder`: Placeholder for user input, optional
    * `access_token`: Access token for user input, optional
    * `request_id`: Unique identifier for request-response matching, optional
    * `route`: Route information for message routing, optional
    """

    type: MessageType
    category: Optional[str] = None
    full_screen: Optional[bool] = False
    project_state_id: Optional[str] = None
    extra_info: Optional[dict] = None
    content: Union[str, dict, None] = None
    placeholder: Optional[str] = None
    accessToken: Optional[str] = None
    request_id: Optional[str] = None
    route: Optional[str] = None

    def to_bytes(self) -> bytes:
        """
        Convert Message instance to wire format.
        """
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_bytes(self, data: bytes) -> "Message":
        """
        Parses raw byte payload into a message.

        This is done in two phases. First, the bytes are UTF-8
        decoded and converted to a dict. Then, the dictionary
        structure is parsed into a Message object.

        This lets us raise different errors based on whether the
        data is not valid JSON or the JSON structure is not valid
        for a Message object.

        :param data: Raw byte payload.
        :return: Message object.
        """
        try:
            json_data = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            raise ValueError(f"Error decoding JSON: {err}") from err
        return Message.model_validate_json(json.dumps(json_data))


class IPCClientUI(UIBase):
    """
    UI adapter for Pythagora VSCode extension IPC.
    """

    def __init__(self, config: LocalIPCConfig):
        """
        Initialize the IPC client with the given configuration.
        """
        self.config = config
        self.reader = None
        self.writer = None

    async def start(self):
        log.debug(f"Connecting to IPC server at {self.config.host}:{self.config.port}")
        try:
            self.reader, self.writer = await asyncio.open_connection(
                self.config.host,
                self.config.port,
                limit=MESSAGE_SIZE_LIMIT,
            )
            return True
        except (ConnectionError, OSError, ConnectionRefusedError) as err:
            log.error(f"Can't connect to the Pythagora VSCode extension: {err}")
            return False

    async def _send(self, type: MessageType, fake: Optional[bool] = False, **kwargs):
        msg = Message(type=type, **kwargs)
        if fake:
            return msg
        else:
            data = msg.to_bytes()
            if self.writer.is_closing():
                log.error("IPC connection closed, can't send the message")
                raise UIClosedError()
            try:
                self.writer.write(len(data).to_bytes(4, byteorder="big"))
                self.writer.write(data)
                await self.writer.drain()
                return msg
            except (ConnectionResetError, BrokenPipeError) as err:
                log.error(f"Connection lost while sending the message: {err}")
                raise UIClosedError()

    async def _receive(self) -> Message:
        data = b""
        while True:
            try:
                response = await self.reader.read(MESSAGE_SIZE_LIMIT)
            except (
                asyncio.exceptions.IncompleteReadError,
                ConnectionResetError,
                asyncio.exceptions.CancelledError,
                BrokenPipeError,
            ):
                raise UIClosedError()

            if response == b"":
                # We're at EOF, the server closed the connection
                raise UIClosedError()

            data += response
            try:
                return Message.from_bytes(data)
            except ValidationError as err:
                # Incorrect payload is most likely a bug in the server, ignore the message
                log.error(f"Error parsing incoming message: {err}", exc_info=True)
                data = b""
                continue
            except ValueError:
                # Most likely, this is as an incomplete message from the server, wait a bit more
                continue

    async def stop(self):
        if not self.writer:
            return

        log.debug(f"Closing the IPC connection to {self.config.host}:{self.config.port}")

        try:
            await self._send(MessageType.EXIT)
            self.writer.close()
            await self.writer.wait_closed()
        except Exception as err:
            log.error(f"Error while closing the connection: {err}", exc_info=True)

        self.writer = None
        self.reader = None

    async def send_stream_chunk(
        self,
        chunk: Optional[str],
        *,
        source: Optional[UISource] = None,
        project_state_id: Optional[str] = None,
        route: Optional[str] = None,
    ):
        if not self.writer:
            return

        if chunk is None:
            chunk = "<__stream_end__>"  # end of stream

        await self._send(
            MessageType.STREAM,
            content=chunk,
            category=source.type_name if source else None,
            project_state_id=project_state_id,
            route=route,
        )

    async def send_user_input_history(
        self,
        message: str,
        source: Optional[UISource] = None,
        project_state_id: Optional[str] = None,
        fake: Optional[bool] = False,
    ):
        return await self._send(
            MessageType.USER_INPUT_HISTORY,
            fake,
            content=message,
            category=source.type_name if source else None,
            project_state_id=project_state_id,
        )

    async def send_message(
        self,
        message: str,
        *,
        source: Optional[UISource] = None,
        project_state_id: Optional[str] = None,
        extra_info: Optional[dict] = None,
        fake: Optional[bool] = False,
    ):
        if not self.writer:
            return None

        log.debug(f"Sending message: [{message.strip()}] from {source.type_name if source else '(none)'}")
        return await self._send(
            MessageType.VERBOSE,
            fake,
            content=message,
            category=source.type_name if source else None,
            project_state_id=project_state_id,
            extra_info=extra_info,
        )

    async def send_key_expired(self, message: Optional[str] = None):
        await self._send(MessageType.KEY_EXPIRED)

    async def send_token_expired(self):
        await self._send(MessageType.TOKEN_EXPIRED)
        response = await self._receive()
        return response.accessToken

    async def send_app_finished(
        self,
        app_id: Optional[str] = None,
        app_name: Optional[str] = None,
        folder_name: Optional[str] = None,
    ):
        await self._send(
            MessageType.APP_FINISHED,
            content={
                "app_id": app_id,
                "app_name": app_name,
                "folder_name": folder_name,
            },
        )

    async def send_feature_finished(
        self,
        app_id: Optional[str] = None,
        app_name: Optional[str] = None,
        folder_name: Optional[str] = None,
    ):
        await self._send(
            MessageType.FEATURE_FINISHED,
            content={
                "app_id": app_id,
                "app_name": app_name,
                "folder_name": folder_name,
            },
        )

    async def ask_question(
        self,
        question: str,
        *,
        buttons: Optional[dict[str, str]] = None,
        default: Optional[str] = None,
        buttons_only: bool = False,
        allow_empty: bool = False,
        full_screen: Optional[bool] = False,
        hint: Optional[str] = None,
        verbose: bool = True,
        initial_text: Optional[str] = None,
        source: Optional[UISource] = None,
        project_state_id: Optional[str] = None,
        extra_info: Optional[dict] = None,
        placeholder: Optional[str] = None,
    ) -> UserInput:
        if not self.writer:
            raise UIClosedError()

        category = source.type_name if source else None

        if not extra_info:
            extra_info = {}

        if hint:
            extra_info["hint"] = hint
        elif verbose:
            extra_info["verbose"] = question

        if buttons:
            buttons_str = "/".join(buttons.values())
            extra_info["buttons"] = buttons_str
            extra_info["buttons_only"] = False
            if buttons_only:
                extra_info["buttons_only"] = True

        if initial_text:
            extra_info["initial_text"] = initial_text

        if full_screen:
            extra_info["full_screen"] = True

        await self._send(
            MessageType.USER_INPUT_REQUEST,
            content=question,
            category=category,
            project_state_id=project_state_id,
            extra_info=extra_info,
            placeholder=placeholder,
        )

        response = await self._receive()

        access_token = response.accessToken

        answer = response.content.strip()
        if answer == "exitPythagoraCore":
            raise KeyboardInterrupt()
        if answer == "interrupt":
            raise UserInterruptError()

        if not answer and default:
            answer = default

        if buttons:
            # Answer matches one of the buttons (or maybe the default if it's a button name)
            if answer in buttons:
                return UserInput(button=answer, text=None, access_token=access_token)
            # VSCode extension only deals with values so we need to check them as well
            value2key = {v: k for k, v in buttons.items()}
            if answer in value2key:
                return UserInput(button=value2key[answer], text=None, access_token=access_token)

        if answer or allow_empty:
            return UserInput(button=None, text=answer, access_token=access_token)

        # Empty answer which we don't allow, treat as user cancelled the input
        return UserInput(cancelled=True, access_token=access_token)

    async def send_project_stage(self, data: dict):
        await self._send(MessageType.STAGE, content=json.dumps(data))

    async def send_epics_and_tasks(
        self,
        epics: list[dict],
        tasks: list[dict],
    ):
        await self._send(
            MessageType.EPICS_AND_TASKS,
            content={
                "epics": epics,
                "tasks": tasks,
            },
        )

    async def send_task_progress(
        self,
        index: int,
        n_tasks: int,
        description: str,
        source: str,
        status: str,
        source_index: int = 1,
        tasks: list[dict] = None,
    ):
        await self._send(
            MessageType.PROGRESS,
            content={
                "task": {
                    "index": index,
                    "num_of_tasks": n_tasks,
                    "description": description,
                    "source": source,
                    "status": status,
                    "source_index": source_index,
                },
                "all_tasks": tasks,
            },
        )

    async def send_modified_files(
        self,
        modified_files: dict[str, str, str],
    ):
        await self._send(
            MessageType.MODIFIED_FILES,
            content={"files": modified_files},
        )

    async def send_step_progress(
        self,
        index: int,
        n_steps: int,
        step: dict,
        task_source: str,
    ):
        await self._send(
            MessageType.PROGRESS,
            content={
                "step": {
                    "index": index,
                    "num_of_steps": n_steps,
                    "step": step,
                    "source": task_source,
                }
            },
        )

    async def send_data_about_logs(
        self,
        data_about_logs: dict,
    ):
        await self._send(
            MessageType.DEBUGGING_LOGS,
            content=data_about_logs,
        )

    async def send_run_command(self, run_command: str):
        await self._send(
            MessageType.RUN_COMMAND,
            content=run_command,
        )

    async def send_app_link(self, app_link: str):
        await self._send(
            MessageType.APP_LINK,
            content=app_link,
        )

    async def open_editor(self, file: str, line: Optional[int] = None, wait_for_response: bool = False):
        if not line:
            pass
        await self._send(
            MessageType.OPEN_FILE,
            content={
                "path": file,  # we assume it's a full path, read the rant in HumanInput.input_required()
                "line": line,
            },
            wait_for_response=wait_for_response,
        )
        if wait_for_response:
            response = await self._receive()
            return response

    async def send_project_info(self, name: str, project_id: str, folder_name: str, created_at: str):
        await self._send(
            MessageType.PROJECT_INFO,
            content={
                "name": name,
                "id": project_id,
                "folderName": folder_name,
                "createdAt": created_at,
            },
            route="broadcast",
        )

    async def set_important_stream(self, important_stream: bool = True):
        await self._send(
            MessageType.IMPORTANT_STREAM,
            content={"status": important_stream},
        )

    async def start_breakdown_stream(self):
        await self._send(
            MessageType.BREAKDOWN_STREAM,
            content={},
        )

    async def send_project_stats(self, stats: dict):
        await self._send(
            MessageType.PROJECT_STATS,
            content=stats,
        )

    async def send_test_instructions(
        self,
        test_instructions: str,
        project_state_id: Optional[str] = None,
        source: Optional[UISource] = None,
        fake: Optional[bool] = False,
    ):
        try:
            log.debug("Sending test instructions")
            parsed_instructions = json.loads(test_instructions)
        except Exception:
            # this is for backwards compatibility with the old format
            parsed_instructions = test_instructions

        return await self._send(
            MessageType.TEST_INSTRUCTIONS,
            fake,
            content={
                "test_instructions": parsed_instructions,
            },
            project_state_id=project_state_id,
            category=source.type_name if source else None,
        )

    async def knowledge_base_update(self, knowledge_base: dict):
        log.debug("Sending updated knowledge base")

        await self._send(
            MessageType.KNOWLEDGE_BASE_UPDATE,
            content={
                "knowledge_base": knowledge_base,
            },
        )

    async def send_file_status(
        self, file_path: str, file_status: str, source: Optional[UISource] = None, fake: Optional[bool] = False
    ):
        return await self._send(
            MessageType.FILE_STATUS,
            fake,
            category=source.type_name if source else None,
            content={
                "file_path": file_path,
                "file_status": file_status,
            },
        )

    async def send_bug_hunter_status(self, status: str, num_cycles: int):
        await self._send(
            MessageType.BUG_HUNTER_STATUS,
            content={
                "status": status,
                "num_cycles": num_cycles,
            },
        )

    async def generate_diff(
        self,
        file_path: str,
        old_content: str,
        new_content: str,
        n_new_lines: int = 0,
        n_del_lines: int = 0,
        source: Optional[UISource] = None,
        fake: Optional[bool] = False,
    ):
        return await self._send(
            MessageType.GENERATE_DIFF,
            fake,
            category=source.type_name if source else None,
            content={
                "file_path": file_path,
                "old_content": old_content,
                "new_content": new_content,
                "n_new_lines": n_new_lines,
                "n_del_lines": n_del_lines,
            },
        )

    async def stop_app(self):
        log.debug("Sending signal to stop the App")
        await self._send(MessageType.STOP_APP)

    async def close_diff(self):
        log.debug("Sending signal to close the generated diff file")
        await self._send(MessageType.CLOSE_DIFF)

    async def loading_finished(self):
        log.debug("Sending project loading finished signal to the extension")
        await self._send(MessageType.LOADING_FINISHED)

    async def send_project_description(self, state: dict):
        await self._send(MessageType.PROJECT_DESCRIPTION, content=state)

    async def send_features_list(self, features: list[str]):
        await self._send(MessageType.FEATURES_LIST, content={"featuresList": features})

    async def import_project(self, project_dir: str):
        await self._send(MessageType.IMPORT_PROJECT, content={"project_dir": project_dir})

    async def send_back_logs(
        self,
        items: list[dict],
    ):
        # Add id field to each item
        for item in items:
            item["id"] = item.get("project_state_id")

        await self._send(MessageType.BACK_LOGS, content={"items": items})

    async def send_fatal_error(
        self,
        message: str,
        extra_info: Optional[dict] = None,
        source: Optional[UISource] = None,
        project_state_id: Optional[str] = None,
    ):
        await self._send(
            MessageType.FATAL_ERROR,
            content=message,
            category=source.type_name if source else None,
            project_state_id=project_state_id,
            extra_info=extra_info,
        )

    async def send_front_logs_headers(
        self,
        project_state_id: str,
        labels: list[str],
        title: str,
        task_id: Optional[str] = None,
    ):
        await self._send(
            MessageType.FRONT_LOGS_HEADERS,
            content={
                "project_state_id": project_state_id,
                "labels": labels,
                "title": title,
                "task_id": task_id,
            },
        )

    async def clear_main_logs(self):
        await self._send(
            MessageType.CLEAR_MAIN_LOGS,
        )

    async def load_front_logs(
        self,
        items: list[object],
    ):
        """
        Load front conversation data to the UI.

        :param items: Array of front logs.
        """
        await self._send(MessageType.LOAD_FRONT_LOGS, content={"items": items})


__all__ = ["IPCClientUI"]
