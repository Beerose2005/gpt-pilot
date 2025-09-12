from enum import Enum
from typing import Optional

from pydantic import BaseModel


class ProjectStage(str, Enum):
    PROJECT_NAME = "project_name"
    PROJECT_DESCRIPTION = "project_description"
    SPECS_STARTED = "specs_started"
    SPECS_FINISHED = "specs_finished"
    FRONTEND_STARTED = "frontend_started"
    FRONTEND_FINISHED = "frontend_finished"
    CONTINUE_FRONTEND = "continue_frontend"
    ITERATE_FRONTEND = "iterate_frontend"
    GET_USER_FEEDBACK = "get_user_feedback"
    OPEN_PLAN = "open_plan"
    STARTING_TASK = "starting_task"
    TASK_FINISHED = "task_finished"
    BREAKDOWN_CHAT = "breakdown_chat"
    TEST_APP = "test_app"
    ADDITIONAL_FEEDBACK = "additional_feedback"
    DESCRIBE_CHANGE = "describe_change"
    DESCRIBE_ISSUE = "describe_issue"
    STARTING_NEW_FEATURE = "starting_new_feature"
    FEATURE_FINISHED = "feature_finished"
    INITIAL_APP_FINISHED = "initial_app_finished"


class UIClosedError(Exception):
    """The user interface has been closed (user stoped Pythagora)."""


class UserInterruptError(Exception):
    """The user interface has been interrupted (user stoped Pythagora)."""


class UISource:
    """
    Source for UI messages.

    See also: `AgentSource`

    Attributes:
    * `display_name`: Human-readable name of the source.
    * `type_name`: Type name of the source (used in IPC)
    """

    display_name: str
    type_name: str

    def __init__(self, display_name: str, type_name: str):
        """
        Create a new UI source.

        :param display_name: Human-readable name of the source.
        :param type_name: Type name of the source (used in IPC)
        """
        self.display_name = display_name
        self.type_name = type_name

    def __str__(self) -> str:
        return self.display_name


class AgentSource(UISource):
    """
    Agent UI source.

    Attributes:
    * `display_name`: Human-readable name of the agent (eg. "Product Owner").
    * `type_name`: Type of the agent (eg. "agent:product-owner").
    """

    def __init__(self, display_name: str, agent_type: str):
        """
        Create a new agent source.

        :param display_name: Human-readable name of the agent.
        :param agent_type: Type of the agent.
        """
        super().__init__(display_name, f"agent:{agent_type}")


class UserInput(BaseModel):
    """
    Represents user input.

    See also: `UIBase.ask_question()`

    Attributes:
    * `text`: User-provided text (if any).
    * `button`: Name (key) of the button the user selected (if any).
    * `cancelled`: Whether the user cancelled the input.
    * `access_token`: Access token (if any).
    """

    text: Optional[str] = None
    button: Optional[str] = None
    cancelled: bool = False
    access_token: Optional[str] = None


class UIBase:
    """
    Base class for UI adapters.
    """

    async def start(self) -> bool:
        """
        Start the UI adapter.

        :return: Whether the UI was started successfully.
        """
        raise NotImplementedError()

    async def stop(self):
        """
        Stop the UI adapter.
        """
        raise NotImplementedError()

    async def send_stream_chunk(
        self,
        chunk: str,
        *,
        source: Optional[UISource] = None,
        project_state_id: Optional[str] = None,
        route: Optional[str] = None,
    ):
        """
        Send a chunk of the stream to the UI.

        :param chunk: Chunk of the stream.
        :param source: Source of the stream (if any).
        :param project_state_id: Current project state id.
        :param route: Route information for message routing.
        """
        raise NotImplementedError()

    async def send_user_input_history(
        self,
        message: str,
        source: Optional[UISource] = None,
        project_state_id: Optional[str] = None,
        fake: Optional[bool] = False,
    ):
        """
        Send a user input history (what the user said to Pythagora) message to the UI.

        :param message: Message content.
        :param source: Source of the message (if any).
        :param project_state_id: Current project state id.
        :param fake: Whether to actually send the message - if fake==True, will not send msg.
        """
        raise NotImplementedError()

    async def send_message(
        self,
        message: str,
        *,
        source: Optional[UISource] = None,
        project_state_id: Optional[str] = None,
        extra_info: Optional[dict] = None,
        fake: Optional[bool] = False,
    ):
        """
        Send a complete message to the UI.

        :param message: Message content.
        :param source: Source of the message (if any).
        :param project_state_id: Current project state id.
        :param extra_info: Extra information to indicate special functionality in extension.
        :param fake: Whether to actually send the message - if fake==True, will not send msg.
        """
        raise NotImplementedError()

    async def send_key_expired(self, message: Optional[str] = None):
        """
        Send the key expired message.
        """
        raise NotImplementedError()

    async def send_token_expired(self):
        """
        Send the token expired message.
        """
        raise NotImplementedError()

    async def send_app_finished(
        self,
        app_id: Optional[str] = None,
        app_name: Optional[str] = None,
        folder_name: Optional[str] = None,
    ):
        """
        Send the app finished message.

        :param app_id: App ID.
        :param app_name: App name.
        :param folder_name: Folder name.
        """
        raise NotImplementedError()

    async def send_feature_finished(
        self,
        app_id: Optional[str] = None,
        app_name: Optional[str] = None,
        folder_name: Optional[str] = None,
    ):
        """
        Send the feature finished message.

        :param app_id: App ID.
        :param app_name: App name.
        :param folder_name: Folder name.
        """
        raise NotImplementedError()

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
        """
        Ask the user a question.

        If buttons are provided, the UI should use the item values
        as button labels, and item keys as the values to return.

        After the user answers, constructs a `UserInput` object
        with the selected button or text. If the user cancels
        the input, the `cancelled` attribute should be set to True.

        :param project_state_id: Current project state id.
        :param initial_text: Placeholder for answer in extension.
        :param hint: Hint for question.
        :param question: Question to ask.
        :param buttons: Buttons to display (if any).
        :param default: Default value (if user provides no input).
        :param buttons_only: Whether to only show buttons (disallow custom text).
        :param allow_empty: Whether to allow empty input.
        :param full_screen: Ask question in full screen (IPC).
        :param verbose: Whether to log the question and response.
        :param source: Source of the question (if any).
        :param extra_info: Extra information to indicate special functionality in extension.
        :param placeholder: Placeholder text for the input field.
        :return: User input.
        """
        raise NotImplementedError()

    async def send_project_stage(self, data: dict):
        """
        Send a project stage to the UI.

        :param data: Project stage data.
        """
        raise NotImplementedError()

    async def send_epics_and_tasks(
        self,
        epics: list[dict] = None,
        tasks: list[dict] = None,
    ):
        """
        Send epics and tasks info to the UI.

        :param epics: List of all epics.
        :param tasks: List of all tasks.
        """
        raise NotImplementedError()

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
        """
        Send a task progress update to the UI.

        :param index: Index of the current task, starting from 1.
        :param n_tasks: Total number of tasks.
        :param description: Description of the task.
        :param source: Source of the task, one of: 'app', 'feature', 'debugger', 'troubleshooting', 'review'.
        :param status: Status of the task, can be 'in_progress' or 'done'.
        :param source_index: Index of the source.
        :param tasks: List of all tasks.
        """
        raise NotImplementedError()

    async def send_step_progress(
        self,
        index: int,
        n_steps: int,
        step: dict,
        task_source: str,
    ):
        """
        Send a step progress update to the UI.

        :param index: Index of the step within the current task, starting from 1.
        :param n_steps: Number of steps in the current task.
        :param step: Step data.
        :param task_source: Source of the task, one of: 'app', 'feature', 'debugger', 'troubleshooting', 'review'.
        """
        raise NotImplementedError()

    async def send_modified_files(
        self,
        modified_files: dict[str, str, str],
    ):
        """
        Send a list of modified files to the UI.

        :param modified_files: List of modified files.
        """
        raise NotImplementedError()

    async def send_data_about_logs(
        self,
        data_about_logs: dict,
    ):
        """
        Send the data about debugging logs.

        :param data_about_logs: Data about logs.
        """
        raise NotImplementedError()

    async def send_run_command(self, run_command: str):
        """
        Send a run command to the UI.

        :param run_command: Run command.
        """
        raise NotImplementedError()

    async def send_app_link(self, app_link: str):
        """
        Send a run command to the UI.

        :param app_link: App link.
        """
        raise NotImplementedError()

    async def open_editor(self, file: str, line: Optional[int] = None, wait_for_response: bool = False):
        """
        Open an editor at the specified file and line.

        :param file: File to open.
        :param line: Line to highlight.
        """
        raise NotImplementedError()

    async def send_project_info(self, name: str, project_id: str, folder_name: str, created_at: str):
        """
        Send project details to the UI.

        :param name: Project name.
        :param project_id: Project ID.
        :param folder_name: Project folder name.
        :param created_at: Project creation date.
        """
        raise NotImplementedError()

    async def set_important_stream(self, important_stream: bool = True):
        """
        Tell the extension that next stream should be visible and rendered as markdown

        """
        raise NotImplementedError()

    async def start_breakdown_stream(self):
        """
        Tell the extension that breakdown stream will start.
        """
        raise NotImplementedError()

    async def send_project_stats(self, stats: dict):
        """
        Send project statistics to the UI.

        The stats object should have the following keys:
        * `num_lines` - Total number of lines in the project
        * `num_files` - Number of files in the project
        * `num_tokens` - Number of tokens used for LLM requests in this session

        :param stats: Project statistics.
        """
        raise NotImplementedError()

    async def send_test_instructions(
        self, test_instructions: str, project_state_id: Optional[str] = None, fake: Optional[bool] = False
    ):
        """
        Send test instructions.

        :param test_instructions: Test instructions.
        :param project_state_id: Project state ID.
        :param fake: Whether to actually send the message - if fake==True, will not send msg.
        """
        raise NotImplementedError()

    async def knowledge_base_update(self, knowledge_base: dict):
        """
        Send updated knowledge base to the UI.

        :param knowledge_base: Knowledge base.
        """
        raise NotImplementedError()

    async def send_file_status(
        self, file_path: str, file_status: str, source: Optional[UISource] = None, fake: Optional[bool] = False
    ):
        """
        Send file status.

        :param file_path: File path.
        :param file_status: File status.
        :param source: Source of the file status.
        :param fake: Whether to actually send the message - if fake==True, will not send msg.
        """
        raise NotImplementedError()

    async def send_bug_hunter_status(self, status: str, num_cycles: int):
        """
        Send bug hunter status.

        :param status: Bug hunter status.
        :param num_cycles: Number of Bug hunter cycles.
        """
        raise NotImplementedError()

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
        """
        Generate a diff between two files.

        :param file_path File path.
        :param old_content: Old file content.
        :param new_content: New file content.
        :param n_new_lines: Number of new lines.
        :param n_del_lines: Number of deleted lines.
        :param source: Source of the diff.
        :param fake: Whether to actually send the message - if fake==True, will not send msg.
        """
        raise NotImplementedError()

    async def stop_app(self):
        """
        Stop the App.
        """
        raise NotImplementedError()

    async def close_diff(self):
        """
        Close all diff views.
        """
        raise NotImplementedError()

    async def loading_finished(self):
        """
        Notify the UI that loading has finished.
        """
        raise NotImplementedError()

    async def send_project_description(self, state: dict):
        """
        Send the project description to the UI.

        :param description: Project description.
        """
        raise NotImplementedError()

    async def send_features_list(self, features: list[str]):
        """
        Send the summaries of implemented features to the UI.

        Features are epics after the initial one (initial project).

        :param features: List of feature summaries.
        """
        raise NotImplementedError()

    async def import_project(self, project_dir: str):
        """
        Ask the UI to import files from the project directory.

        The UI should provide a way for the user to select the directory with
        existing project, and recursively copy the files over.

        :param project_dir: Project directory.
        """
        raise NotImplementedError()

    async def send_back_logs(
        self,
        items: list[dict],
    ):
        """
        Send background conversation data to the UI.

        :param items: List of conversation objects, each containing:
                      - project_state_id: string
                      - labels: array of strings
                      - title: string
                      - convo: array of objects
        """
        raise NotImplementedError()

    async def send_fatal_error(
        self,
        message: str,
        extra_info: Optional[dict] = None,
        source: Optional[UISource] = None,
        project_state_id: Optional[str] = None,
    ):
        pass

    async def send_front_logs_headers(
        self,
        project_state_id: str,
        labels: list[str],
        title: str,
        task_id: Optional[str] = None,
    ):
        """
        Send front conversation data to the UI.

        :param project_state_id: Project state ID.
        :param labels: Array of label strings.
        :param title: Conversation title.
        :param task_id: Task ID.
        """
        raise NotImplementedError()

    async def clear_main_logs(self):
        """
        Send message to clear main logs.
        """
        raise NotImplementedError()


pythagora_source = UISource("Pythagora", "pythagora")
success_source = UISource("Congratulations", "success")


__all__ = [
    "UISource",
    "AgentSource",
    "UserInput",
    "UIBase",
    "pythagora_source",
    "success_source",
]
