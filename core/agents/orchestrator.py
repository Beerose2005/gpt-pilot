import asyncio
import json
import os
import re
from typing import List, Optional, Union

from core.agents.architect import Architect
from core.agents.base import BaseAgent
from core.agents.bug_hunter import BugHunter
from core.agents.code_monkey import CodeMonkey
from core.agents.developer import Developer
from core.agents.error_handler import ErrorHandler
from core.agents.executor import Executor
from core.agents.external_docs import ExternalDocumentation
from core.agents.frontend import Frontend
from core.agents.git import GitMixin
from core.agents.human_input import HumanInput
from core.agents.importer import Importer
from core.agents.legacy_handler import LegacyHandler
from core.agents.problem_solver import ProblemSolver
from core.agents.response import AgentResponse, ResponseType
from core.agents.spec_writer import SpecWriter
from core.agents.task_completer import TaskCompleter
from core.agents.tech_lead import TechLead
from core.agents.tech_writer import TechnicalWriter
from core.agents.troubleshooter import Troubleshooter
from core.agents.wizard import Wizard
from core.db.models.project_state import IterationStatus, TaskStatus
from core.log import get_logger
from core.telemetry import telemetry
from core.ui.base import UserInterruptError

log = get_logger(__name__)


class Orchestrator(BaseAgent, GitMixin):
    """
    Main agent that controls the flow of the process.

    Based on the current state of the project, the orchestrator invokes
    all other agents. It is also responsible for determining when each
    step is done and the project state needs to be committed to the database.
    """

    agent_type = "orchestrator"
    display_name = "Orchestrator"

    async def run(self) -> bool:
        """
        Run the Orchestrator agent.

        :return: True if the Orchestrator exited successfully, False otherwise.
        """
        response = None

        log.info(f"Starting {__name__}.Orchestrator")

        self.executor = Executor(self.state_manager, self.ui)
        self.process_manager = self.executor.process_manager
        # self.chat = Chat() TODO

        await self.init_ui()
        await self.offline_changes_check()

        await self.install_dependencies()

        if self.args.use_git and await self.check_git_installed():
            await self.init_git_if_needed()

        await self.set_frontend_script()
        await self.set_package_json()
        await self.set_vite_config()
        await self.set_favicon()
        await self.enable_debugger()
        await self.ui.knowledge_base_update(
            {
                "pages": self.current_state.knowledge_base.pages,
                "apis": self.current_state.knowledge_base.apis,
                "user_options": self.current_state.knowledge_base.user_options,
                "utility_functions": self.current_state.knowledge_base.utility_functions,
            }
        )

        # TODO: consider refactoring this into two loop; the outer with one iteration per committed step,
        # and the inner which runs the agents for the current step until they're done. This would simplify
        # handle_done() and let us do other per-step processing (eg. describing files) in between agent runs.
        while True:
            # If the task is marked as "redo_human_instructions", we need to reload the project at the state before the current task breakdown
            if (
                self.current_state.current_task
                and self.current_state.current_task.get("redo_human_instructions", None) is not None
            ):
                redo_human_instructions = self.current_state.current_task["redo_human_instructions"]
                project_state = await self.state_manager.get_project_state_for_redo_task(self.current_state)

                if project_state is not None:
                    await self.state_manager.load_project(
                        branch_id=project_state.branch_id, step_index=project_state.step_index
                    )
                    await self.state_manager.restore_files()
                    self.current_state.epics[-1]["completed"] = False
                    self.next_state.epics[-1]["completed"] = False
                    self.next_state.current_task["redo_human_instructions"] = redo_human_instructions

            await self.update_stats()
            agent = self.create_agent(response)
            # In case where agent is a list, run all agents in parallel.
            # Only one agent type can be run in parallel at a time (for now). See handle_parallel_responses().
            if isinstance(agent, list):
                tasks = [single_agent.run() for single_agent in agent]
                log.debug(
                    f"Running agents {[a.__class__.__name__ for a in agent]} (step {self.current_state.step_index})"
                )
                responses = await asyncio.gather(*tasks)
                response = self.handle_parallel_responses(agent[0], responses)

                should_update_knowledge_base = any(
                    "src/pages/" in single_agent.step.get("save_file", {}).get("path", "")
                    or "src/api/" in single_agent.step.get("save_file", {}).get("path", "")
                    or single_agent.current_state.current_task.get("related_api_endpoints")
                    for single_agent in agent
                )

                if should_update_knowledge_base:
                    files_with_implemented_apis = [
                        {
                            "path": single_agent.step.get("save_file", {}).get("path", None),
                            "content": single_agent.step.get("save_file", {}).get("content", None),
                            "related_api_endpoints": single_agent.current_state.current_task.get(
                                "related_api_endpoints"
                            ),
                            "line": 0,
                        }
                        for single_agent in agent
                        if single_agent.current_state.current_task.get("related_api_endpoints")
                    ]

                    await self.state_manager.update_apis(files_with_implemented_apis)
                    await self.state_manager.update_implemented_pages_and_apis()

            else:
                log.debug(f"Running agent {agent.__class__.__name__} (step {self.current_state.step_index})")
                try:
                    response = await agent.run()
                except UserInterruptError:
                    log.debug("User interrupted the agent!")
                    response = AgentResponse.done(self)

            if response.type == ResponseType.EXIT:
                log.debug(f"Agent {agent.__class__.__name__} requested exit")
                break

            if response.type == ResponseType.DONE:
                response = await self.handle_done(agent, response)
                log.debug(f"Agent {agent.__class__.__name__} returned")
                if not isinstance(agent, list) and agent.agent_type == "spec-writer":
                    project_details = self.state_manager.get_project_info()
                    await self.ui.send_project_info(
                        project_details["name"],
                        project_details["id"],
                        project_details["folderName"],
                        project_details["createdAt"],
                    )
                continue

        # TODO: rollback changes to "next" so they aren't accidentally committed?
        return True

    async def install_dependencies(self):
        # First check if package.json exists
        package_json_path = os.path.join(self.state_manager.get_full_project_root(), "package.json")
        if not os.path.exists(package_json_path):
            # Skip if no package.json found
            return

        # Then check if node_modules directory exists
        node_modules_path = os.path.join(self.state_manager.get_full_project_root(), "node_modules")
        if not os.path.exists(node_modules_path):
            await self.send_message("Installing project dependencies...")
            await self.process_manager.run_command("npm install", show_output=False, timeout=600)

    async def set_frontend_script(self):
        file_path = os.path.join("client", "index.html")
        absolute_path = os.path.join(self.state_manager.get_full_project_root(), file_path)
        script_tag = '<script src="https://s3.us-east-1.amazonaws.com/assets.pythagora.ai/scripts/utils.js"></script>'

        # Check if file exists
        if not os.path.exists(absolute_path):
            return

        try:
            # Read the HTML file
            with open(absolute_path, "r", encoding="utf-8") as file:
                content = file.read()

            # Check if script already exists
            if script_tag in content:
                return

            # Find the head tag and title tag
            head_match = re.search(r"<head[^>]*>(.*?)</head>", content, re.DOTALL | re.IGNORECASE)

            if head_match:
                head_content = head_match.group(1)
                title_match = re.search(r"(<title[^>]*>.*?</title>)", head_content, re.DOTALL | re.IGNORECASE)

                if title_match:
                    # Insert after title
                    new_head = head_content.replace(title_match.group(1), f"{title_match.group(1)}\n    {script_tag}")
                else:
                    # Insert at the beginning of head
                    new_head = f"\n    {script_tag}{head_content}"

                # Replace old head content with new one
                new_content = content.replace(head_content, new_head)

                await self.state_manager.save_file(file_path, new_content)

        except Exception as e:
            log.error(f"An error occurred: {str(e)}")

    async def enable_debugger(self):
        absolute_path = os.path.join(self.state_manager.get_full_project_root(), "package.json")

        if not os.path.exists(absolute_path):
            return

        try:
            with open(absolute_path, "r") as file:
                package_json = json.load(file)

            if "debug" not in package_json["scripts"]:
                package_json["scripts"]["debug"] = (
                    'concurrently -n "client,server" "npm run client" "cross-env NODE_OPTIONS=--inspect-brk=9229 npm run server"'
                )

            if "devDependencies" not in package_json:
                package_json["devDependencies"] = {}

                if "cross-env" not in package_json["devDependencies"]:
                    package_json["devDependencies"]["cross-env"] = "^7.0.3"
            else:
                return
            await self.state_manager.save_file(absolute_path, json.dumps(package_json))
            await self.process_manager.run_command("npm install", show_output=True, timeout=600)

            log.debug("Debugger support added.")

        except Exception as e:
            log.debug(f"An error occurred: {e}")

    async def set_favicon(self):
        """
        Set up favicon link in the client/index.html file.
        """
        try:
            client_dir = os.path.join(self.state_manager.get_full_project_root(), "client")
            index_path = os.path.join(client_dir, "index.html")

            if not os.path.exists(index_path):
                return

            # Read the HTML file
            with open(index_path, "r", encoding="utf-8") as file:
                content = file.read()

            favicon_link = '<link rel="icon" type="image/x-icon" href="https://s3.us-east-1.amazonaws.com/assets.pythagora.ai/logos/favicon.ico" />'

            # Check if favicon link already exists
            if favicon_link in content:
                return

            # Find the position where to insert the favicon link
            # Look for </head> tag and insert before it
            updated_content = content.replace("</head>", f"  {favicon_link}\n  </head>")

            await self.state_manager.save_file(index_path, updated_content)
            log.debug("Favicon link added to index.html")

        except Exception as e:
            log.debug(f"An error occurred while setting favicon: {e}")

    async def set_package_json(self):
        file_path = os.path.join("client", "package.json")
        absolute_path = os.path.join(self.state_manager.get_full_project_root(), file_path)

        if not os.path.exists(absolute_path):
            return

        try:
            script = "vite build"
            with open(absolute_path, "r") as file:
                package_json = json.load(file)

            if package_json["scripts"].get("build") == script:
                return

            package_json["scripts"]["build"] = script

            await self.state_manager.save_file(absolute_path, json.dumps(package_json, indent=4))
            log.debug(f"Build script changed to {script}.")

        except Exception as e:
            log.debug(f"An error occurred: {e}")

    async def set_vite_config(self):
        file_path = os.path.join("client", "vite.config.ts")
        absolute_path = os.path.join(self.state_manager.get_full_project_root(), file_path)

        if not os.path.exists(absolute_path):
            return

        try:
            # Read the current file
            with open(absolute_path, "r", encoding="utf-8") as file:
                current_content = file.read()

            # Check if required configs already exist
            has_host_true = "host: true" in current_content
            has_watch_config = "watch: {" in current_content and "ignored:" in current_content

            # If both required configs exist, no need to change anything
            if has_host_true and has_watch_config:
                log.debug("Vite config already has host:true and watch configuration. No changes needed.")
                return

            # Get the template path
            project_root = self.state_manager.get_full_project_root()
            base_path = project_root.split("/pythagora-core")[0] + "/pythagora-core"
            template_path = os.path.join(
                base_path, "core", "templates", "tree", "vite_react", "client", "vite.config.ts"
            )

            # Read the template file
            with open(template_path, "r", encoding="utf-8") as file:
                template_content = file.read()

            # Save the template content to the target file
            await self.state_manager.save_file(file_path, template_content)
            log.debug("Updated vite.config.ts with the template configuration.")

        except Exception as e:
            log.debug(f"An error occurred while updating vite.config.ts: {e}")

    def handle_parallel_responses(self, agent: BaseAgent, responses: List[AgentResponse]) -> AgentResponse:
        """
        Handle responses from agents that were run in parallel.

        This method is called when multiple agents are run in parallel, and it
        should return a single response that represents the combined responses
        of all agents.

        :param agent: The original agent that was run in parallel.
        :param responses: List of responses from all agents.
        :return: Combined response.
        """
        response = AgentResponse.done(agent)
        if isinstance(agent, CodeMonkey):
            files = []
            for single_response in responses:
                if single_response.type == ResponseType.INPUT_REQUIRED:
                    files += single_response.data.get("files", [])
                    break
            if files:
                response = AgentResponse.input_required(agent, files)
            return response
        else:
            raise ValueError(f"Unhandled parallel agent type: {agent.__class__.__name__}")

    async def offline_changes_check(self):
        """
        Check for changes outside Pythagora.

        If there are changes, ask the user if they want to keep them, and
        import if needed.
        """

        try:
            log.info("Checking for offline changes.")
            modified_files = await self.state_manager.get_modified_files_with_content()

            if self.state_manager.workspace_is_empty():
                # NOTE: this will currently get triggered on a new project, but will do
                # nothing as there's no files in the database.
                log.info("Detected empty workspace, restoring state from the database.")
                await self.state_manager.restore_files()
            elif modified_files:
                await self.send_message(f"We found {len(modified_files)} new and/or modified files.")
                await self.ui.send_modified_files(modified_files)
                hint = "".join(
                    [
                        "If you would like Pythagora to import those changes, click 'Yes'.\n",
                        "Clicking 'No' means Pythagora will restore (overwrite) all files to the last stored state.\n",
                    ]
                )
                use_changes = await self.ask_question(
                    question="Would you like to keep your changes?",
                    buttons={
                        "yes": "Yes, keep my changes",
                        "no": "No, restore last Pythagora state",
                    },
                    buttons_only=True,
                    hint=hint,
                )
                if use_changes.button == "yes":
                    log.debug("Importing offline changes into Pythagora.")
                    await self.import_files()
                else:
                    log.debug("Restoring last stored state.")
                    await self.state_manager.restore_files()

            log.info("Offline changes check done.")
        except UserInterruptError:
            await self.state_manager.restore_files()
            log.debug("User interrupted the offline changes check, restoring files.")
            return

    async def handle_done(self, agent: BaseAgent, response: AgentResponse) -> AgentResponse:
        """
        Handle the DONE response from the agent and commit current state to the database.

        This also checks for any files created or modified outside Pythagora and
        imports them. If any of the files require input from the user, the returned response
        will trigger the HumanInput agent to ask the user to provide the required input.

        """
        if self.next_state and self.next_state.tasks:
            n_epics = len(self.next_state.epics)
            n_finished_epics = n_epics - len(self.next_state.unfinished_epics)
            n_tasks = len(self.next_state.tasks)
            n_finished_tasks = n_tasks - len(self.next_state.unfinished_tasks)
            n_iterations = len(self.next_state.iterations)
            n_finished_iterations = n_iterations - len(self.next_state.unfinished_iterations)
            n_steps = len(self.next_state.steps)
            n_finished_steps = n_steps - len(self.next_state.unfinished_steps)

            log.debug(
                f"Agent {agent.__class__.__name__} is done, "
                f"committing state for step {self.current_state.step_index}: "
                f"{n_finished_epics}/{n_epics} epics, "
                f"{n_finished_tasks}/{n_tasks} tasks, "
                f"{n_finished_iterations}/{n_iterations} iterations, "
                f"{n_finished_steps}/{n_steps} dev steps."
            )

        await self.state_manager.commit()

        # If there are any new or modified files changed outside Pythagora,
        # this is a good time to add them to the project. If any of them have
        # INPUT_REQUIRED, we'll first ask the user to provide the required input.
        import_files_response = await self.import_files()

        # If any of the files are missing metadata/descriptions, those need to be filled-in
        missing_descriptions = [
            file.path for file in self.current_state.files if not file.content.meta.get("description")
        ]
        if missing_descriptions:
            log.debug(f"Some files are missing descriptions: {', '.join(missing_descriptions)}, requesting analysis")
            return AgentResponse.describe_files(self)

        return import_files_response

    def create_agent(self, prev_response: Optional[AgentResponse]) -> Union[List[BaseAgent], BaseAgent]:
        state = self.current_state

        if prev_response:
            if prev_response.type in [ResponseType.CANCEL, ResponseType.ERROR]:
                return ErrorHandler(self.state_manager, self.ui, prev_response=prev_response)
            if prev_response.type == ResponseType.DESCRIBE_FILES:
                return CodeMonkey(self.state_manager, self.ui, prev_response=prev_response)
            if prev_response.type == ResponseType.INPUT_REQUIRED:
                # FIXME: HumanInput should be on the whole time and intercept chat/interrupt
                return HumanInput(self.state_manager, self.ui, prev_response=prev_response)
            if prev_response.type == ResponseType.IMPORT_PROJECT:
                return Importer(self.state_manager, self.ui, prev_response=prev_response)
            if prev_response.type == ResponseType.EXTERNAL_DOCS_REQUIRED:
                return ExternalDocumentation(self.state_manager, self.ui, prev_response=prev_response)
            if prev_response.type == ResponseType.UPDATE_SPECIFICATION:
                return SpecWriter(self.state_manager, self.ui, prev_response=prev_response, args=self.args)

        if not state.epics:
            return Wizard(self.state_manager, self.ui, process_manager=self.process_manager)
        elif state.epics and not state.epics[0].get("description"):
            # New project: ask the Spec Writer to refine and save the project specification
            return SpecWriter(self.state_manager, self.ui, process_manager=self.process_manager, args=self.args)
        elif state.current_epic and state.current_epic.get("source") == "frontend":
            # Build frontend
            return Frontend(self.state_manager, self.ui, process_manager=self.process_manager)
        elif not state.specification.architecture:
            # Ask the Architect to design the project architecture and determine dependencies
            return Architect(self.state_manager, self.ui, process_manager=self.process_manager)
        elif not self.current_state.unfinished_tasks or (state.specification.templates and not state.files):
            # Ask the Tech Lead to break down the initial project or feature into tasks and apply project templates
            return TechLead(self.state_manager, self.ui, process_manager=self.process_manager)

        # Current task status must be checked before Developer is called because we might want
        # to skip it instead of breaking it down
        current_task_status = state.current_task.get("status") if state.current_task else None
        if current_task_status:
            # Status of the current task is set first time after the task was reviewed by user
            log.info(f"Status of current task: {current_task_status}")
            if current_task_status == TaskStatus.REVIEWED:
                # User reviewed the task, call TechnicalWriter to see if documentation needs to be updated
                return TechnicalWriter(self.state_manager, self.ui)
            elif current_task_status in [TaskStatus.DOCUMENTED, TaskStatus.SKIPPED]:
                # Task is fully done or skipped, call TaskCompleter to mark it as completed
                return TaskCompleter(self.state_manager, self.ui, process_manager=self.process_manager)

        if not state.steps and not state.iterations:
            # Ask the Developer to break down current task into actionable steps
            return Developer(self.state_manager, self.ui)

        if state.current_step:
            # Execute next step in the task
            # TODO: this can be parallelized in the future
            return self.create_agent_for_step(state.current_step)

        if state.unfinished_iterations:
            current_iteration_status = state.current_iteration["status"]
            if current_iteration_status == IterationStatus.HUNTING_FOR_BUG:
                # Triggering the bug hunter to start the hunt
                return BugHunter(self.state_manager, self.ui)
            elif current_iteration_status == IterationStatus.START_PAIR_PROGRAMMING:
                # Pythagora cannot solve the issue so we're starting pair programming
                return BugHunter(self.state_manager, self.ui)
            elif current_iteration_status == IterationStatus.AWAITING_LOGGING:
                # Get the developer to implement logs needed for debugging
                return Developer(self.state_manager, self.ui)
            elif current_iteration_status == IterationStatus.AWAITING_BUG_FIX:
                # Get the developer to implement the bug fix for debugging
                return Developer(self.state_manager, self.ui)
            elif current_iteration_status == IterationStatus.IMPLEMENT_SOLUTION:
                # Get the developer to implement the "change" requested by the user
                return Developer(self.state_manager, self.ui)
            elif current_iteration_status == IterationStatus.AWAITING_USER_TEST:
                # Getting the bug hunter to ask the human to test the bug fix
                return BugHunter(self.state_manager, self.ui)
            elif current_iteration_status == IterationStatus.AWAITING_BUG_REPRODUCTION:
                # Getting the bug hunter to ask the human to reproduce the bug
                return BugHunter(self.state_manager, self.ui)
            elif current_iteration_status == IterationStatus.FIND_SOLUTION:
                # Find solution to the iteration problem
                return Troubleshooter(self.state_manager, self.ui)
            elif current_iteration_status == IterationStatus.PROBLEM_SOLVER:
                # Call Problem Solver if the user said "I'm stuck in a loop"
                return ProblemSolver(self.state_manager, self.ui)
            elif current_iteration_status == IterationStatus.NEW_FEATURE_REQUESTED:
                # Call Spec Writer to add the "change" requested by the user to project specification
                return SpecWriter(self.state_manager, self.ui, args=self.args)

        # We have just finished the task, call Troubleshooter to ask the user to review
        return Troubleshooter(self.state_manager, self.ui)

    def create_agent_for_step(self, step: dict) -> Union[List[BaseAgent], BaseAgent]:
        step_type = step.get("type")
        if step_type == "save_file":
            steps = self.current_state.get_steps_of_type("save_file")
            parallel = []
            for step in steps:
                parallel.append(CodeMonkey(self.state_manager, self.ui, step=step))
            return parallel
        elif step_type == "command":
            return self.executor.for_step(step)
        elif step_type == "human_intervention":
            return HumanInput(self.state_manager, self.ui, step=step)
        elif step_type == "review_task":
            return LegacyHandler(self.state_manager, self.ui, data={"type": "review_task"})
        elif step_type == "create_readme":
            return TechnicalWriter(self.state_manager, self.ui)
        elif step_type == "utility_function":
            return Developer(self.state_manager, self.ui)
        else:
            raise ValueError(f"Unknown step type: {step_type}")

    async def import_files(self) -> Optional[AgentResponse]:
        imported_files, removed_paths = await self.state_manager.import_files()
        if not imported_files and not removed_paths:
            return None

        if imported_files:
            log.info(f"Imported new/changed files to project: {', '.join(f.path for f in imported_files)}")
        if removed_paths:
            log.info(f"Removed files from project: {', '.join(removed_paths)}")

        input_required_files: list[dict[str, int]] = []
        for file in imported_files:
            for line in self.state_manager.get_input_required(file.content.content, file.path):
                input_required_files.append({"file": file.path, "line": line})

        if input_required_files:
            # This will trigger the HumanInput agent to ask the user to provide the required changes
            # If the user changes anything (removes the "required changes"), the file will be re-imported.
            return AgentResponse.input_required(self, input_required_files)

        # Commit the newly imported file
        log.debug(f"Committing imported/removed files as a separate step {self.current_state.step_index}")
        await self.state_manager.commit()
        return None

    async def init_ui(self):
        project_details = self.state_manager.get_project_info()
        await self.ui.send_project_info(
            project_details["name"], project_details["id"], project_details["folderName"], project_details["createdAt"]
        )
        await self.ui.loading_finished()

        if self.current_state.epics:
            if len(self.current_state.epics) > 3:
                # We only want to send previous features, ie. exclude current one and the initial project (first epic)
                await self.ui.send_features_list([e["description"] for e in self.current_state.epics[2:-1]])

        if self.current_state.specification.description:
            await self.ui.send_project_description(
                {
                    "project_description": self.current_state.specification.description,
                    "project_type": self.current_state.branch.project.project_type,
                }
            )

    async def update_stats(self):
        if self.current_state.steps and self.current_state.current_step:
            source = self.current_state.current_step.get("source")
            source_steps = self.current_state.get_last_iteration_steps()
            await self.ui.send_step_progress(
                source_steps.index(self.current_state.current_step) + 1,
                len(source_steps),
                self.current_state.current_step,
                source,
            )

        total_files = 0
        total_lines = 0
        for file in self.current_state.files:
            total_files += 1
            total_lines += len(file.content.content.splitlines())

        telemetry.set("num_files", total_files)
        telemetry.set("num_lines", total_lines)

        stats = telemetry.get_project_stats()
        await self.ui.send_project_stats(stats)
