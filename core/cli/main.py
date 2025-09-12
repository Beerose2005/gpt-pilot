import asyncio
import atexit
import gc
import signal
import sys
import traceback
from argparse import Namespace
from asyncio import run

from core.config.actions import FE_ITERATION_DONE

try:
    import sentry_sdk

    SENTRY_AVAILABLE = True
except ImportError:
    SENTRY_AVAILABLE = False

from core.agents.orchestrator import Orchestrator
from core.cli.helpers import (
    capture_exception,
    delete_project,
    init,
    init_sentry,
    list_projects_branches_states,
    list_projects_json,
    load_convo,
    load_project,
    print_convo,
    show_config,
)
from core.db.session import SessionManager
from core.db.v0importer import LegacyDatabaseImporter
from core.llm.anthropic_client import CustomAssertionError
from core.llm.base import APIError
from core.log import get_logger
from core.state.state_manager import StateManager
from core.telemetry import telemetry
from core.ui.api_server import IPCServer
from core.ui.base import (
    UIBase,
    UIClosedError,
    pythagora_source,
)

log = get_logger(__name__)

telemetry_sent = False


async def cleanup(ui: UIBase):
    global telemetry_sent
    if not telemetry_sent:
        await telemetry.send()
        telemetry_sent = True
    await ui.stop()


def sync_cleanup(ui: UIBase):
    asyncio.run(cleanup(ui))


async def run_project(sm: StateManager, ui: UIBase, args) -> bool:
    """
    Work on the project.

    Starts the orchestrator agent with the newly loaded/created project
    and runs it until the orchestrator decides to exit.

    :param sm: State manager.
    :param ui: User interface.
    :param args: Command-line arguments.
    :return: True if the orchestrator exited successfully, False otherwise.
    """

    telemetry.set("app_id", str(sm.project.id))
    telemetry.set("initial_prompt", sm.current_state.specification.description)

    orca = Orchestrator(sm, ui, args=args)
    success = False
    try:
        success = await orca.run()
        telemetry.set("end_result", "success:exit" if success else "failure:api-error")
    except (KeyboardInterrupt, UIClosedError):
        log.info("Interrupted by user")
        telemetry.set("end_result", "interrupt")
        await sm.rollback()
    except APIError as err:
        log.warning(f"an LLM API error occurred: {err.message}")
        await send_error(ui, "error while calling the LLM API", err)
        telemetry.set("end_result", "failure:api-error")
        await sm.rollback()
    except CustomAssertionError as err:
        log.warning(f"an Anthropic assertion error occurred: {str(err)}")
        await send_error(ui, "error inside Anthropic SDK", err)
        telemetry.set("end_result", "failure:assertion-error")
        await sm.rollback()
    except Exception as err:
        log.error(f"Uncaught exception: {err}", exc_info=True)
        await send_error(ui, "an error", err)

        telemetry.record_crash(err)
        await sm.rollback()

    return success


async def send_error(ui: UIBase, error_source: str, err: Exception):
    stack_trace = traceback.format_exc()
    await ui.send_fatal_error(
        f"Stopping Pythagora due to {error_source}:\n\n{err}",
        source=pythagora_source,
        extra_info={
            "fatal_error": True,
            "stack_trace": stack_trace,
        },
    )
    capture_exception(err)


async def start_new_project(sm: StateManager, ui: UIBase, args: Namespace = None) -> bool:
    """
    Start a new project.

    :param sm: State manager.
    :param ui: User interface.
    :param args: Command-line arguments.
    :return: True if the project was created successfully, False otherwise.
    """

    # Check if initial_prompt is provided, if so, automatically select "node"
    if args and args.initial_prompt:
        stack_button = "node"

        await ui.send_back_logs(
            [
                {
                    "title": "",
                    "project_state_id": "spec",
                    "labels": [""],
                    "convo": [{"role": "assistant", "content": "Please describe the app you want to build."}],
                }
            ]
        )
    else:
        stack = await ui.ask_question(
            "What do you want to build?",
            allow_empty=False,
            buttons={
                "node": "Full stack app\n(easiest to get started)",
                "swagger": "Frontend only\n(if you have backend with OpenAPI\\Swagger)",
            },
            buttons_only=True,
            source=pythagora_source,
            full_screen=True,
        )

        await ui.send_back_logs(
            [
                {
                    "title": "",
                    "project_state_id": "setup",
                    "labels": [""],
                    "convo": [{"role": "assistant", "content": "What do you want to build?"}],
                }
            ]
        )

        if stack.button == "other":
            language = await ui.ask_question(
                "What language you want to use?",
                allow_empty=False,
                source=pythagora_source,
                full_screen=True,
            )
            await telemetry.trace_code_event(
                "stack-choice-other",
                {"language": language.text},
            )
            await ui.send_message("Thank you for submitting your request to support other languages.")
            return False

        stack_button = stack.button

    await telemetry.trace_code_event(
        "stack-choice",
        {"language": stack_button},
    )

    project_state = await sm.create_project(project_type=stack_button)
    return project_state is not None


async def run_pythagora_session(sm: StateManager, ui: UIBase, args: Namespace):
    """
    Run a Pythagora session.

    :param sm: State manager.
    :param ui: User interface.
    :param args: Command-line arguments.
    :return: True if the application ran successfully, False otherwise.
    """

    if args.project or args.branch or args.step or args.project_state_id:
        telemetry.set("is_continuation", True)
        sm.fe_auto_debug = False
        project_state = await load_project(sm, args.project, args.branch, args.step, args.project_state_id)
        if not project_state:
            return False

        # SPECIFICATION
        fe_states = await sm.get_fe_states(limit=10)
        be_back_logs, last_task_in_db = await sm.get_be_back_logs()

        if sm.current_state.specification:
            if not sm.current_state.specification.original_description:
                spec = sm.current_state.specification
                spec.description = project_state.epics[0]["description"]
                spec.original_description = project_state.epics[0]["description"]
                await sm.update_specification(spec)

            await ui.send_front_logs_headers(
                "",
                ["E1 / T1", "Writing Specification", "working" if fe_states == [] else "done"],
                "Writing Specification",
                "",
            )
            await ui.send_back_logs(
                [
                    {
                        "project_state_id": "spec",
                        "disallow_reload": True,
                        "labels": ["E1 / T1", "Specs", "working" if fe_states == [] else "done"],
                        "title": "Writing Specification",
                        "convo": [
                            {
                                "role": "assistant",
                                "content": "What do you want to build?",
                            },
                            {
                                "role": "user",
                                "content": sm.current_state.specification.original_description,
                            },
                        ],
                        "start_id": "",
                        "end_id": "",
                    }
                ]
            )
            if not fe_states and be_back_logs and not last_task_in_db:
                await ui.send_message(
                    sm.current_state.specification.description,
                    extra_info={"route": "forwardToCenter", "screen": "spec"},
                )

        # FRONTEND

        if fe_states:
            status = "working" if fe_states[-1].action != FE_ITERATION_DONE else "done"
            await ui.send_front_logs_headers(fe_states[0].id, ["E2 / T1", "Frontend", status], "Building Frontend", "")
            await ui.send_back_logs(
                [
                    {
                        "labels": ["E2 / T1", "Frontend", status],
                        "title": "Building Frontend",
                        "convo": [],
                        "project_state_id": fe_states[0].id,
                        "start_id": fe_states[0].id,
                        "end_id": fe_states[-1].id,
                    }
                ]
            )

        # BACKEND
        if be_back_logs:
            await ui.send_back_logs(be_back_logs)

        if not be_back_logs and not last_task_in_db:
            # if no backend logs AND no task is currently active -> we are on frontend -> print frontend convo history
            convo = await load_convo(sm, project_states=fe_states)
            await print_convo(ui=ui, convo=convo, fake=False)
            # Clear fe_states from memory after conversation is loaded
            del fe_states
            gc.collect()  # Force garbage collection to free memory immediately
        elif last_task_in_db:
            # Clear fe_states from memory as they're not needed for backend processing
            del fe_states
            gc.collect()  # Force garbage collection to free memory immediately

            # if there is a task in the db (we are at backend stage), print backend convo history and add task back logs and front logs headers
            await ui.send_front_logs_headers(
                last_task_in_db["start_id"],
                last_task_in_db["labels"],
                last_task_in_db["title"],
                last_task_in_db.get("task_id", ""),
            )
            await ui.send_back_logs(
                [
                    {
                        "project_state_id": last_task_in_db["start_id"],
                        "labels": last_task_in_db["labels"],
                        "title": last_task_in_db["title"],
                        "convo": [],
                        "start_id": last_task_in_db["start_id"],
                        "end_id": last_task_in_db["end_id"],
                    }
                ]
            )
            be_states = await sm.get_project_states_in_between(last_task_in_db["start_id"], last_task_in_db["end_id"])
            convo = await load_convo(sm, project_states=be_states)
            await print_convo(ui=ui, convo=convo, fake=False)
            # Clear be_states from memory after conversation is loaded
            del be_states
            gc.collect()  # Force garbage collection to free memory immediately

    else:
        sm.fe_auto_debug = True
        success = await start_new_project(sm, ui, args)
        if not success:
            return False
    return await run_project(sm, ui, args)


async def async_main(
    ui: UIBase,
    db: SessionManager,
    args: Namespace,
) -> bool:
    """
    Main application coroutine.

    :param ui: User interface.
    :param db: Database session manager.
    :param args: Command-line arguments.
    :return: True if the application ran successfully, False otherwise.
    """
    global telemetry_sent

    if args.list:
        await list_projects_branches_states(db)
        return True
    elif args.list_json:
        await list_projects_json(db)
        return True
    if args.show_config:
        show_config()
        return True
    elif args.import_v0:
        importer = LegacyDatabaseImporter(db, args.import_v0)
        await importer.import_database()
        return True
    elif args.delete:
        success = await delete_project(db, args.delete)
        return success

    telemetry.set("user_contact", args.email)

    if SENTRY_AVAILABLE and args.email:
        init_sentry()
        sentry_sdk.set_user({"email": args.email})

    if args.extension_version:
        log.debug(f"Extension version: {args.extension_version}")
        telemetry.set("is_extension", True)
        telemetry.set("extension_version", args.extension_version)

    sm = StateManager(db, ui)
    if args.access_token:
        sm.update_access_token(args.access_token)

    # Start API server if enabled in config
    api_server = None
    if hasattr(args, "enable_api_server") and args.enable_api_server:
        api_host = getattr(args, "local_api_server_host", "localhost")
        api_port = getattr(args, "local_api_server_port", 8222)  # Different from client port
        api_server = IPCServer(api_host, api_port, sm)
        server_started = await api_server.start()
        if not server_started:
            log.warning(f"Failed to start API server on {api_host}:{api_port}")

    if not args.auto_confirm_breakdown:
        sm.auto_confirm_breakdown = False
    ui_started = await ui.start()
    if not ui_started:
        if api_server:
            await api_server.stop()
        return False

    telemetry.start()

    def signal_handler(sig, frame):
        try:
            loop = asyncio.get_running_loop()

            def close_all():
                loop.stop()
                sys.exit(0)

            if not telemetry_sent:
                cleanup_task = loop.create_task(cleanup(ui))
                cleanup_task.add_done_callback(close_all)
            else:
                close_all()
        except RuntimeError:
            if not telemetry_sent:
                sync_cleanup(ui)
            sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, signal_handler)

    # Register the cleanup function
    atexit.register(sync_cleanup, ui)

    try:
        success = await run_pythagora_session(sm, ui, args)
    except Exception as err:
        log.error(f"Uncaught exception in main session: {err}", exc_info=True)
        await send_error(ui, "an error", err)
        raise
    finally:
        await cleanup(ui)
        if api_server:
            await api_server.stop()

    return success


def run_pythagora():
    ui, db, args = init()
    if not ui or not db:
        return -1
    success = run(async_main(ui, db, args))
    return 0 if success else -1


if __name__ == "__main__":
    sys.exit(run_pythagora())
