import json
import os
import os.path
import sys
from argparse import ArgumentParser, ArgumentTypeError, Namespace
from difflib import unified_diff
from typing import Optional
from urllib.parse import urlparse
from uuid import UUID

from core.config import Config, LLMProvider, LocalIPCConfig, ProviderConfig, UIAdapter, get_config, loader
from core.config.actions import (
    BH_ADDITIONAL_FEEDBACK,
    BH_HUMAN_TEST_AGAIN,
    BH_IS_BUG_FIXED,
    BH_START_BUG_HUNT,
    BH_START_USER_TEST,
    BH_STARTING_PAIR_PROGRAMMING,
    BH_WAIT_BUG_REP_INSTRUCTIONS,
    CM_UPDATE_FILES,
    DEV_EXECUTE_TASK,
    DEV_TASK_BREAKDOWN,
    DEV_TASK_START,
    DEV_TROUBLESHOOT,
    FE_CHANGE_REQ,
    FE_DONE_WITH_UI,
    HUMAN_INTERVENTION_QUESTION,
    MIX_BREAKDOWN_CHAT_PROMPT,
    RUN_COMMAND,
    TC_TASK_DONE,
    TL_EDIT_DEV_PLAN,
    TS_APP_WORKING,
    TS_DESCRIBE_ISSUE,
)
from core.config.env_importer import import_from_dotenv
from core.config.version import get_version
from core.db.models import ProjectState
from core.db.models.project_state import TaskStatus
from core.db.session import SessionManager
from core.db.setup import run_migrations
from core.llm.parser import DescriptiveCodeBlockParser
from core.log import get_logger, setup
from core.state.state_manager import StateManager
from core.ui.base import AgentSource, UIBase, UISource
from core.ui.console import PlainConsoleUI
from core.ui.ipc_client import IPCClientUI
from core.ui.virtual import VirtualUI
from core.utils.text import trim_logs

log = get_logger(__name__)

try:
    import sentry_sdk
    from sentry_sdk.integrations.asyncio import AsyncioIntegration

    SENTRY_AVAILABLE = True
except ImportError:
    SENTRY_AVAILABLE = False


def parse_llm_endpoint(value: str) -> Optional[tuple[LLMProvider, str]]:
    """
    Parse --llm-endpoint command-line option.

    Option syntax is: --llm-endpoint <provider>:<url>

    :param value: Argument value.
    :return: Tuple with LLM provider and URL, or None if the option wasn't provided.
    """
    if not value:
        return None

    parts = value.split(":", 1)
    if len(parts) != 2:
        raise ArgumentTypeError("Invalid LLM endpoint format; expected 'provider:url'")

    try:
        provider = LLMProvider(parts[0])
    except ValueError as err:
        raise ArgumentTypeError(f"Unsupported LLM provider: {err}")
    url = urlparse(parts[1])
    if url.scheme not in ("http", "https"):
        raise ArgumentTypeError(f"Invalid LLM endpoint URL: {parts[1]}")

    return provider, url.geturl()


def get_line_changes(old_content: str, new_content: str) -> tuple[int, int]:
    """
    Get the number of added and deleted lines between two files.

    This uses Python difflib to produce a unified diff, then counts
    the number of added and deleted lines.

    :param old_content: old file content
    :param new_content: new file content
    :return: a tuple (added_lines, deleted_lines)
    """

    from_lines = old_content.splitlines(keepends=True)
    to_lines = new_content.splitlines(keepends=True)

    diff_gen = unified_diff(from_lines, to_lines)

    added_lines = 0
    deleted_lines = 0

    for line in diff_gen:
        if line.startswith("+") and not line.startswith("+++"):  # Exclude the file headers
            added_lines += 1
        elif line.startswith("-") and not line.startswith("---"):  # Exclude the file headers
            deleted_lines += 1

    return added_lines, deleted_lines


def calculate_pr_changes(convo_entries):
    """
    Calculate file changes between initial and final versions, similar to a Git pull request.

    This function tracks the initial (first seen) and final (last seen) versions of each file,
    and calculates the diff between those two versions, ignoring intermediate changes.

    :param convo_entries: List of conversation entries containing file changes
    :return: List of file changes with initial and final versions
    """
    file_changes = {}

    for entry in convo_entries:
        if not entry.get("files"):
            continue

        for file_data in entry.get("files", []):
            path = file_data.get("path")
            if not path:
                continue

            if path not in file_changes:
                file_changes[path] = {
                    "path": path,
                    "old_content": file_data.get("old_content", ""),
                    "new_content": file_data.get("new_content", ""),
                }
            else:
                file_changes[path]["new_content"] = file_data.get("new_content", "")

    for path, file_info in file_changes.items():
        file_info["n_new_lines"], file_info["n_del_lines"] = get_line_changes(
            old_content=file_info["old_content"], new_content=file_info["new_content"]
        )

    # Convert dict to list
    return list(file_changes.values())


def parse_llm_key(value: str) -> Optional[tuple[LLMProvider, str]]:
    """
    Parse --llm-key command-line option.

    Option syntax is: --llm-key <provider>:<key>

    :param value: Argument value.
    :return: Tuple with LLM provider and key, or None if if the option wasn't provided.
    """
    if not value:
        return None

    parts = value.split(":", 1)
    if len(parts) != 2:
        raise ArgumentTypeError("Invalid LLM endpoint format; expected 'provider:key'")

    try:
        provider = LLMProvider(parts[0])
    except ValueError as err:
        raise ArgumentTypeError(f"Unsupported LLM provider: {err}")

    return provider, parts[1]


def parse_arguments() -> Namespace:
    """
    Parse command-line arguments.

    Available arguments:
        --help: Show the help message
        --config: Path to the configuration file
        --show-config: Output the default configuration to stdout
        --default-config: Output the configuration to stdout
        --level: Log level (debug,info,warning,error,critical)
        --database: Database URL
        --local-ipc-port: Local IPC port to connect to
        --local-ipc-host: Local IPC host to connect to
        --version: Show the version and exit
        --list: List all projects
        --list-json: List all projects in JSON format
        --project: Load a specific project
        --branch: Load a specific branch
        --step: Load a specific step in a project/branch
        --llm-endpoint: Use specific API endpoint for the given provider
        --llm-key: Use specific LLM key for the given provider
        --import-v0: Import data from a v0 (gpt-pilot) database with the given path
        --email: User's email address, if provided
        --extension-version: Version of the VSCode extension, if used
        --use-git: Use Git for version control
        --access-token: Access token
        --initial-prompt: Initial prompt to automatically start a new project with 'node' stack
    :return: Parsed arguments object.
    """
    version = get_version()

    parser = ArgumentParser()
    parser.add_argument("--config", help="Path to the configuration file", default="config.json")
    parser.add_argument("--show-config", help="Output the default configuration to stdout", action="store_true")
    parser.add_argument("--level", help="Log level (debug,info,warning,error,critical)", required=False)
    parser.add_argument("--database", help="Database URL", required=False)
    parser.add_argument("--local-ipc-port", help="Local IPC port to connect to", type=int, required=False)
    parser.add_argument("--local-ipc-host", help="Local IPC host to connect to", default="localhost", required=False)
    parser.add_argument("--version", action="version", version=version)
    parser.add_argument("--list", help="List all projects", action="store_true")
    parser.add_argument("--list-json", help="List all projects in JSON format", action="store_true")
    parser.add_argument("--project", help="Load a specific project", type=UUID, required=False)
    parser.add_argument("--branch", help="Load a specific branch", type=UUID, required=False)
    parser.add_argument("--step", help="Load a specific step in a project/branch", type=int, required=False)
    parser.add_argument(
        "--project-state-id", help="Load a specific project state in a project/branch", type=UUID, required=False
    )
    parser.add_argument("--delete", help="Delete a specific project", type=UUID, required=False)
    parser.add_argument(
        "--llm-endpoint",
        help="Use specific API endpoint for the given provider",
        type=parse_llm_endpoint,
        action="append",
        required=False,
    )
    parser.add_argument(
        "--llm-key",
        help="Use specific LLM key for the given provider",
        type=parse_llm_key,
        action="append",
        required=False,
    )
    parser.add_argument(
        "--import-v0",
        help="Import data from a v0 (gpt-pilot) database with the given path",
        required=False,
    )
    parser.add_argument("--email", help="User's email address", required=False)
    parser.add_argument("--extension-version", help="Version of the VSCode extension", required=False)
    parser.add_argument("--use-git", help="Use Git for version control", action="store_true", required=False)
    parser.add_argument(
        "--no-auto-confirm-breakdown",
        help="Disable auto confirm when LLM requests user input",
        action="store_false",
        dest="auto_confirm_breakdown",
        required=False,
    )
    parser.add_argument("--access-token", help="Access token", required=False)
    parser.add_argument(
        "--enable-api-server",
        action="store_true",
        default=True,
        help="Enable IPC server for external clients",
    )
    parser.add_argument(
        "--local-api-server-host",
        type=str,
        default="localhost",
        help="Host for the IPC server (default: localhost)",
    )
    parser.add_argument(
        "--local-api-server-port",
        type=int,
        default=8222,
        help="Port for the IPC server (default: 8222)",
    )
    parser.add_argument(
        "--initial-prompt",
        help="Initial prompt to automatically start a new project with 'node' stack",
        required=False,
    )
    return parser.parse_args()


def load_config(args: Namespace) -> Optional[Config]:
    """
    Load Pythagora JSON configuration file and apply command-line arguments.

    :param args: Command-line arguments (at least `config` must be present).
    :return: Configuration object, or None if config couldn't be loaded.
    """
    if not os.path.isfile(args.config):
        imported = import_from_dotenv(args.config)
        if not imported:
            print(f"Configuration file not found: {args.config}; using default", file=sys.stderr)
            return get_config()

    try:
        config = loader.load(args.config)
    except ValueError as err:
        print(f"Error parsing config file {args.config}: {err}", file=sys.stderr)
        return None

    if args.level:
        config.log.level = args.level.upper()

    if args.database:
        config.db.url = args.database

    if args.local_ipc_port:
        config.ui = LocalIPCConfig(port=args.local_ipc_port, host=args.local_ipc_host)

    if args.llm_endpoint:
        for provider, endpoint in args.llm_endpoint:
            if provider not in config.llm:
                config.llm[provider] = ProviderConfig()
            config.llm[provider].base_url = endpoint

    if args.llm_key:
        for provider, key in args.llm_key:
            if provider not in config.llm:
                config.llm[provider] = ProviderConfig()
            config.llm[provider].api_key = key

    try:
        Config.model_validate(config)
    except ValueError as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return None

    return config


async def list_projects_json(db: SessionManager):
    """
    List all projects in the database in JSON format.
    """
    sm = StateManager(db)
    projects = await sm.list_projects()
    projects_list = []
    for row in projects:
        project_id, project_name, created_at, folder_name = row
        projects_list.append(
            {
                "id": project_id.hex,
                "name": project_name,
                "folder_name": folder_name,
                "updated_at": created_at.isoformat(),
            }
        )

    print(json.dumps(projects_list, indent=2, default=str))


def insert_new_task(tasks, new_task):
    # Find the index of the first task with status "todo"
    todo_index = -1
    for i, task in enumerate(tasks):
        if task.get("status") == TaskStatus.TODO:
            todo_index = i
            break

    if todo_index != -1:
        tasks.insert(todo_index, new_task)
    else:
        tasks.append(new_task)

    return tasks


def find_task_by_id(tasks, task_id):
    """
    Find a task by its ID from a list of tasks.

    :param tasks: List of task objects
    :param task_id: Task ID to search for
    :return: Task object if found, None otherwise
    """
    for task in tasks:
        if task.get("id") == task_id:
            return task

    return None


def change_order_of_task(tasks, task_to_move, new_position):
    # Remove the task from its current position
    tasks.remove(task_to_move)

    # Insert the task at the new position
    tasks.insert(new_position, task_to_move)

    return tasks


def find_first_todo_task(tasks):
    """
    Find the first task with status 'todo' from a list of tasks.

    :param tasks: List of task objects
    :return: First task with status 'todo', or None if not found
    """
    if not tasks:
        return None

    for task in tasks:
        if task.get("status") == "todo":
            return task

    return None


def find_first_todo_task_index(tasks):
    for i, task in enumerate(tasks):
        if task["status"] == TaskStatus.TODO:
            return i
    return -1


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


def get_source_for_history(msg_type: Optional[str] = "", question: Optional[str] = ""):
    if question in [TL_EDIT_DEV_PLAN]:
        return AgentSource("Tech Lead", "tech-lead")

    if question in [FE_CHANGE_REQ, FE_DONE_WITH_UI]:
        return AgentSource("Frontend", "frontend")

    elif question in [
        TS_DESCRIBE_ISSUE,
        BH_HUMAN_TEST_AGAIN,
        BH_IS_BUG_FIXED,
        TS_APP_WORKING,
        BH_ADDITIONAL_FEEDBACK,
    ] or msg_type in ["instructions", "bh_breakdown"]:
        return AgentSource("Bug Hunter", "bug-hunter")

    elif msg_type in ["bug_reproduction_instructions", "bug_description"]:
        return AgentSource("Troubleshooter", "troubleshooter")

    elif msg_type in ["frontend"]:
        return AgentSource("Frontend", "frontend")

    elif HUMAN_INTERVENTION_QUESTION in question:
        return AgentSource("Human Input", "human-input")

    elif RUN_COMMAND in question:
        return AgentSource("Executor", "executor")

    elif msg_type in ["task_description", "task_breakdown"]:
        return AgentSource("Developer", "developer")

    else:
        return UISource("Pythagora", "pythagora")


"""
Prints the conversation history to the UI.

:param ui: UI instance to send messages to
:param convo: List of conversation messages to print
:param fake: If True, messages will NOT be sent to the extension.
"""


async def print_convo(ui: UIBase, convo: list, fake: Optional[bool] = True):
    msgs = []
    for msg in convo:
        if "frontend" in msg:
            frontend_data = msg["frontend"]
            if isinstance(frontend_data, list):
                for frontend_msg in frontend_data:
                    msgs.append(
                        await ui.send_message(
                            frontend_msg,
                            source=get_source_for_history(msg_type="frontend"),
                            project_state_id=msg["id"],
                            fake=fake,
                        )
                    )
            else:
                msgs.append(
                    await ui.send_message(
                        frontend_data,
                        source=get_source_for_history(msg_type="frontend"),
                        project_state_id=msg["id"],
                        fake=fake,
                    )
                )

        if "bh_breakdown" in msg:
            msgs.append(
                await ui.send_message(
                    msg["bh_breakdown"],
                    source=get_source_for_history(msg_type="bh_breakdown"),
                    project_state_id=msg["id"],
                    fake=fake,
                )
            )

        if "task_description" in msg:
            msgs.append(
                await ui.send_message(
                    msg["task_description"],
                    source=get_source_for_history(msg_type="task_description"),
                    project_state_id=msg["id"],
                    fake=fake,
                )
            )

        if "task_breakdown" in msg:
            msgs.append(
                await ui.send_message(
                    msg["task_breakdown"],
                    source=get_source_for_history(msg_type="task_breakdown"),
                    project_state_id=msg["id"],
                    fake=fake,
                )
            )

        if "test_instructions" in msg:
            msgs.append(
                await ui.send_test_instructions(msg["test_instructions"], project_state_id=msg["id"], fake=fake)
            )

        if "bh_testing_instructions" in msg:
            msgs.append(
                await ui.send_test_instructions(msg["bh_testing_instructions"], project_state_id=msg["id"], fake=fake)
            )

        if "files" in msg:
            for f in msg["files"]:
                msgs.append(await ui.send_file_status(f["path"], "done", fake=fake))
                msgs.append(
                    await ui.generate_diff(
                        file_path=f["path"],
                        old_content=f.get("old_content", ""),
                        new_content=f.get("new_content", ""),
                        n_new_lines=f["diff"][0],
                        n_del_lines=f["diff"][1],
                        fake=fake,
                    )
                )

        if "user_inputs" in msg and msg["user_inputs"]:
            for input_item in msg["user_inputs"]:
                if "question" in input_item:
                    msgs.append(
                        await ui.send_message(
                            input_item["question"],
                            source=get_source_for_history(question=input_item["question"]),
                            project_state_id=msg["id"],
                            fake=fake,
                        )
                    )

                if "answer" in input_item:
                    if input_item["question"] != TL_EDIT_DEV_PLAN:
                        msgs.append(
                            await ui.send_user_input_history(
                                input_item["answer"], project_state_id=msg["id"], fake=fake
                            )
                        )

    return msgs


async def load_convo(
    sm: StateManager,
    project_id: Optional[UUID] = None,
    branch_id: Optional[UUID] = None,
    project_states: Optional[list] = None,
) -> list:
    """
    Loads the conversation from an existing project.
    returns: list of dictionaries with the conversation history
    """
    convo = []

    if not branch_id:
        branch_id = sm.current_state.branch_id

    if project_states is not None and len(project_states) == 0:
        return convo

    if not project_states:
        project_states = await sm.get_project_states(project_id, branch_id)

    task_counter = 1
    fe_printed_msgs = []
    fe_commands = []

    for i, state in enumerate(project_states):
        convo_el = {}
        convo_el["id"] = str(state.id) if state.step_index >= 3 else None
        user_inputs = await sm.find_user_input(state, branch_id)

        if state.tasks and state.current_task:
            task_counter = state.tasks.index(state.current_task) + 1

        if user_inputs:
            convo_el["user_inputs"] = []
            for ui in user_inputs:
                if ui.question:
                    if ui.question == MIX_BREAKDOWN_CHAT_PROMPT:
                        if len(state.iterations) > 0:
                            # as it's not available in the current state, take the next state's description - that is the bug description!
                            next_state = project_states[i + 1] if i + 1 < len(project_states) else None
                            if next_state is not None and next_state.iterations is not None:
                                si = next_state.iterations[-1]
                                if si is not None:
                                    if si.get("description", None) is not None:
                                        convo_el["bh_breakdown"] = si["description"]
                        else:
                            # if there are no iterations, it means developer made task breakdown, take the next state's first task with status = todo
                            next_state = project_states[i + 1] if i + 1 < len(project_states) else None
                            if next_state is not None:
                                task = find_first_todo_task(next_state.tasks)
                                if task and task.get("test_instructions", None) is not None:
                                    convo_el["test_instructions"] = task["test_instructions"]
                                if task and task.get("instructions", None) is not None:
                                    convo_el["task_breakdown"] = task["instructions"]
                        # skip parsing that questions and its answers due to the fact that we do not keep states inside breakdown convo
                        break

                    if ui.question == BH_HUMAN_TEST_AGAIN:
                        if len(state.iterations) > 0:
                            si = state.iterations[-1]
                            if si is not None:
                                if si.get("bug_reproduction_description", None) is not None:
                                    convo_el["bh_testing_instructions"] = si["bug_reproduction_description"]

                    if ui.question == TS_APP_WORKING:
                        task = find_first_todo_task(state.tasks)
                        if task:
                            if task.get("test_instructions", None) is not None:
                                convo_el["test_instructions"] = task["test_instructions"]

                    if ui.question == DEV_EXECUTE_TASK:
                        task = find_first_todo_task(state.tasks)
                        if task:
                            if task.get("description", None) is not None:
                                convo_el["task_description"] = f"Task #{task_counter} - " + task["description"]

                    answer = trim_logs(ui.answer_text) if ui.answer_text is not None else ui.answer_button
                    if answer == "bug":
                        answer = "There is an issue"
                    elif answer == "change":
                        answer = "I want to make a change"
                    convo_el["user_inputs"].append({"question": ui.question, "answer": answer})

        if len(state.epics[-1].get("messages", [])) > 0:
            if convo_el.get("user_inputs", None) is None:
                convo_el["user_inputs"] = []
            parser = DescriptiveCodeBlockParser()

            for msg in state.epics[-1].get("messages", []):
                if msg.get("role") == "assistant" and msg["content"] not in fe_printed_msgs:
                    if "frontend" not in convo_el:
                        convo_el["frontend"] = []
                    convo_el["frontend"].append(msg["content"])
                    fe_printed_msgs.append(msg["content"])

                    blocks = parser(msg.get("content", ""))
                    files_dict = {}
                    for block in blocks.blocks:
                        description = block.description.strip()
                        content = block.content.strip()

                        # Split description into lines and check the last line for file path
                        description_lines = description.split("\n")
                        last_line = description_lines[-1].strip()

                        if "file:" in last_line:
                            # Extract file path from the last line - get everything after "file:"
                            file_path = last_line[last_line.index("file:") + 5 :].strip()
                            file_path = file_path.strip("\"'`")
                            # Skip empty file paths
                            if file_path.strip() == "":
                                continue
                            new_content = content
                            old_content = sm.current_state.get_file_content_by_path(file_path)

                            diff = get_line_changes(
                                old_content=old_content,
                                new_content=new_content,
                            )

                            if diff != (0, 0):
                                files_dict[file_path] = {
                                    "path": file_path,
                                    "old_content": old_content,
                                    "new_content": new_content,
                                    "diff": diff,
                                }

                        elif "command:" in last_line:
                            commands = content.strip().split("\n")
                            for command in commands:
                                command = command.strip()
                                if command and command not in fe_commands:
                                    if "user_inputs" not in convo_el:
                                        convo_el["user_inputs"] = []
                                    convo_el["user_inputs"].append(
                                        {
                                            "question": f"{RUN_COMMAND} {command}",
                                            "answer": "yes",
                                        }
                                    )
                                    fe_commands.append(command)

                    convo_el["files"] = list(files_dict.values())

        if state.action is not None:
            if state.action == DEV_TROUBLESHOOT.format(task_counter):
                if state.iterations is not None and len(state.iterations) > 0:
                    si = state.iterations[-1]
                    if si is not None:
                        if si.get("user_feedback", None) is not None:
                            convo_el["user_feedback"] = si["user_feedback"]
                        if si.get("description", None) is not None:
                            convo_el["description"] = si["description"]

            elif state.action == DEV_TASK_BREAKDOWN.format(task_counter):
                if state.tasks and len(state.tasks) >= task_counter:
                    task = state.current_task
                    if task.get("description", None) is not None:
                        convo_el["task_description"] = f"Task #{task_counter} - " + task["description"]

                    if task.get("instructions", None) is not None:
                        convo_el["task_breakdown"] = task["instructions"]

            elif state.action == TC_TASK_DONE.format(task_counter):
                if state.tasks:
                    next_task = find_first_todo_task(state.tasks)
                    if next_task is not None and next_task.get("description", None) is not None:
                        convo_el["task_description"] = f"Task #{task_counter} - " + next_task["description"]

            elif state.action == DEV_TASK_START.format(task_counter):
                if state.tasks and len(state.tasks) >= task_counter:
                    task = state.current_task
                    if task.get("instructions", None) is not None:
                        convo_el["task_breakdown"] = task["instructions"]

            elif state.action == CM_UPDATE_FILES:
                files_dict = {}
                for steps in state.steps:
                    if "save_file" in steps and "path" in steps["save_file"]:
                        path = steps["save_file"]["path"]

                        current_file = await sm.get_file_for_project(state.id, path)
                        prev_file = (
                            await sm.get_file_for_project(state.prev_state_id, path)
                            if state.prev_state_id is not None
                            else None
                        )

                        old_content = prev_file.content.content if prev_file and prev_file.content else ""
                        new_content = current_file.content.content if current_file and current_file.content else ""

                        diff = get_line_changes(
                            old_content=old_content,
                            new_content=new_content,
                        )

                        # Only add file if it has changes
                        if diff != (0, 0):
                            files_dict[path] = {
                                "path": path,
                                "old_content": old_content,
                                "new_content": new_content,
                                "diff": diff,
                                "bug_hunter": len(state.iterations) > 0
                                and len(state.iterations[-1].get("bug_hunting_cycles", [])) > 0,
                            }

                convo_el["files"] = list(files_dict.values())

            if state.iterations is not None and len(state.iterations) > 0:
                si = state.iterations[-1]

                if state.action == BH_START_BUG_HUNT.format(task_counter):
                    if si.get("user_feedback", None) is not None:
                        convo_el["user_feedback"] = si["user_feedback"]

                    if si.get("description", None) is not None:
                        convo_el["description"] = si["description"]

                elif state.action == BH_WAIT_BUG_REP_INSTRUCTIONS.format(task_counter):
                    for si in state.iterations:
                        if si.get("bug_reproduction_description", None) is not None:
                            convo_el["bug_reproduction_description"] = si["bug_reproduction_description"]

                elif state.action == BH_START_USER_TEST.format(task_counter):
                    if si.get("bug_hunting_cycles", None) is not None:
                        cycle = si["bug_hunting_cycles"][-1]
                        if cycle is not None:
                            if "user_feedback" in cycle and cycle["user_feedback"] is not None:
                                convo_el["user_feedback"] = cycle["user_feedback"]
                            if (
                                "human_readable_instructions" in cycle
                                and cycle["human_readable_instructions"] is not None
                            ):
                                convo_el["human_readable_instructions"] = cycle["human_readable_instructions"]

                elif state.action == BH_STARTING_PAIR_PROGRAMMING.format(task_counter):
                    if "user_feedback" in si and si["user_feedback"] is not None:
                        convo_el["user_feedback"] = si["user_feedback"]
                    if "initial_explanation" in si and si["initial_explanation"] is not None:
                        convo_el["initial_explanation"] = si["initial_explanation"]

        convo_el["action"] = state.action
        convo.append(convo_el)

    return convo


def init_sentry():
    if SENTRY_AVAILABLE:
        sentry_sdk.init(
            dsn="https://4101633bc5560bae67d6eab013ba9686@o4508731634221056.ingest.us.sentry.io/4508732401909760",
            send_default_pii=True,
            traces_sample_rate=1.0,
            integrations=[AsyncioIntegration()],
        )


def capture_exception(exc: Exception):
    if SENTRY_AVAILABLE:
        init_sentry()
        sentry_sdk.capture_exception(exc)


async def list_projects_branches_states(db: SessionManager):
    """
    List all projects in the database, including their branches and project states
    """
    sm = StateManager(db)
    projects = await sm.list_projects_with_branches_states()

    print(f"Available projects ({len(projects)}):")
    for project in projects:
        print(f"* {project.name} ({project.id})")
        for branch in project.branches:
            last_step = max(state.step_index for state in branch.states)
            print(f"  - {branch.name} ({branch.id}) - last step: {last_step}")


async def load_project(
    sm: StateManager,
    project_id: Optional[UUID] = None,
    branch_id: Optional[UUID] = None,
    step_index: Optional[int] = None,
    project_state_id: Optional[UUID] = None,
) -> Optional[ProjectState]:
    """
    Load a project from the database.

    :param sm: State manager.
    :param project_id: Project ID (optional, loads the last step in the main branch).
    :param branch_id: Branch ID (optional, loads the last step in the branch).
    :param step_index: Step index (optional, loads the state at the given step).
    :return: True if the project was loaded successfully, False otherwise.
    """
    step_txt = f" step {step_index}" if step_index else ""

    if branch_id:
        project_state = await sm.load_project(
            branch_id=branch_id, step_index=step_index, project_state_id=project_state_id
        )
        if project_state:
            return project_state
        else:
            print(f"Branch {branch_id}{step_txt} not found; use --list to list all projects", file=sys.stderr)
            return None

    elif project_id:
        project_state = await sm.load_project(
            project_id=project_id, step_index=step_index, project_state_id=project_state_id
        )
        if project_state:
            return project_state
        else:
            print(f"Project {project_id}{step_txt} not found; use --list to list all projects", file=sys.stderr)
            return None

    return None


async def delete_project(db: SessionManager, project_id: UUID) -> bool:
    """
    Delete a project from a database.

    :param sm: State manager.
    :param project_id: Project ID.
    :return: True if project was deleted, False otherwise.
    """

    sm = StateManager(db)
    return await sm.delete_project(project_id)


def show_config():
    """
    Print the current configuration to stdout.
    """
    cfg = get_config()
    print(cfg.model_dump_json(indent=2))


def init() -> tuple[UIBase, SessionManager, Namespace]:
    """
    Initialize the application.

    Loads configuration, sets up logging and UI, initializes the database
    and runs database migrations.

    :return: Tuple with UI, db session manager, file manager, and command-line arguments.
    """
    args = parse_arguments()
    config = load_config(args)
    if not config:
        return (None, None, args)

    setup(config.log, force=True)

    if config.ui.type == UIAdapter.IPC_CLIENT:
        ui = IPCClientUI(config.ui)
    elif config.ui.type == UIAdapter.VIRTUAL:
        ui = VirtualUI(config.ui.inputs)
    else:
        ui = PlainConsoleUI()

    run_migrations(config.db)
    db = SessionManager(config.db, args)

    return (ui, db, args)


__all__ = [
    "parse_arguments",
    "load_config",
    "list_projects_json",
    "list_projects_branches_states",
    "load_project",
    "init",
]
