import asyncio
from uuid import uuid4

from pydantic import BaseModel, Field

from core.agents.base import BaseAgent
from core.agents.convo import AgentConvo
from core.agents.mixins import RelevantFilesMixin
from core.agents.response import AgentResponse
from core.config import TECH_LEAD_EPIC_BREAKDOWN, TECH_LEAD_PLANNING
from core.config.actions import (
    TL_CREATE_INITIAL_EPIC,
    TL_CREATE_PLAN,
    TL_INITIAL_PROJECT_NAME,
    TL_START_FEATURE,
)
from core.db.models import Complexity
from core.db.models.project_state import TaskStatus
from core.llm.parser import JSONParser
from core.log import get_logger
from core.telemetry import telemetry
from core.templates.registry import PROJECT_TEMPLATES
from core.ui.base import ProjectStage, pythagora_source, success_source
from core.utils.text import trim_logs

log = get_logger(__name__)


class APIEndpoint(BaseModel):
    description: str = Field(description="Description of an API endpoint.")
    method: str = Field(description="HTTP method of the API endpoint.")
    endpoint: str = Field(description="URL of the API endpoint.")
    request_body: dict = Field(description="Request body of the API endpoint.")
    response_body: dict = Field(description="Response body of the API endpoint.")


class Epic(BaseModel):
    description: str = Field(description="Description of an epic.")


class Task(BaseModel):
    description: str = Field(description="Description of a task.")
    related_api_endpoints: list[APIEndpoint] = Field(description="API endpoints that will be implemented in this task.")
    testing_instructions: str = Field(description="Instructions for testing the task.")


class DevelopmentPlan(BaseModel):
    plan: list[Epic] = Field(description="List of epics that need to be done to implement the entire plan.")


class EpicPlan(BaseModel):
    plan: list[Task] = Field(description="List of tasks that need to be done to implement the entire epic.")


class TechLead(RelevantFilesMixin, BaseAgent):
    agent_type = "tech-lead"
    display_name = "Tech Lead"

    async def run(self) -> AgentResponse:
        # Building frontend is the first epic
        if len(self.current_state.epics) == 1:
            await self.remove_mocked_data()
            self.create_initial_project_epic()
            return AgentResponse.done(self)

        # if self.current_state.specification.templates and len(self.current_state.files) < 2:
        #     await self.apply_project_templates()
        #     self.next_state.action = "Apply project templates"
        #     await self.ui.send_epics_and_tasks(
        #         self.next_state.current_epic["sub_epics"],
        #         self.next_state.tasks,
        #     )
        #
        #     inputs = []
        #     for file in self.next_state.files:
        #         input_required = self.state_manager.get_input_required(file.content.content)
        #         if input_required:
        #             inputs += [{"file": file.path, "line": line} for line in input_required]
        #
        #     if inputs:
        #         return AgentResponse.input_required(self, inputs)
        #     else:
        #         return AgentResponse.done(self)

        if self.current_state.current_epic:
            await self.remove_mocked_data()
            self.next_state.action = "Create a development plan"
            return await self.plan_epic(self.current_state.current_epic)
        else:
            return await self.ask_for_new_feature()

    def create_initial_project_epic(self):
        self.next_state.action = TL_CREATE_INITIAL_EPIC
        log.debug("Creating initial project Epic")
        self.next_state.epics = self.current_state.epics + [
            {
                "id": uuid4().hex,
                "name": TL_INITIAL_PROJECT_NAME,
                "source": "app",
                "description": self.current_state.specification.description,
                "test_instructions": None,
                "summary": None,
                "completed": False,
                "complexity": self.current_state.specification.complexity,
                "sub_epics": [],
            }
        ]
        self.next_state.relevant_files = None
        self.next_state.modified_files = {}

    async def apply_project_templates(self):
        state = self.current_state
        summaries = []

        # Only do this for the initial project and if the templates are specified
        if len(state.epics) != 1 or not state.specification.templates:
            return

        for template_name, template_options in state.specification.templates.items():
            template_class = PROJECT_TEMPLATES.get(template_name)
            if not template_class:
                log.error(f"Project template not found: {template_name}")
                continue

            template = template_class(
                template_options,
                self.state_manager,
                self.process_manager,
            )

            description = template.description
            log.info(f"Applying project template: {template.name}")
            await self.send_message(f"Applying project template {description} ...")
            summary = await template.apply()
            summaries.append(summary)

        # Saving template files will fill this in and we want it clear for the first task.
        self.next_state.relevant_files = None

        if summaries:
            spec = self.current_state.specification.clone()
            spec.template_summary = "\n\n".join(summaries)

            self.next_state.specification = spec

    async def ask_for_new_feature(self) -> AgentResponse:
        if len(self.current_state.epics) > 2:
            await self.ui.send_message("Your new feature is complete!", source=success_source)
            await self.ui.send_project_stage(
                {
                    "stage": ProjectStage.FEATURE_FINISHED,
                    "feature_number": len(self.current_state.epics),
                }
            )
        else:
            await self.ui.send_message("Your app is DONE! You can start using it right now!", source=success_source)
            await self.ui.send_project_stage(
                {
                    "stage": ProjectStage.INITIAL_APP_FINISHED,
                }
            )

        if self.current_state.run_command:
            await self.ui.send_run_command(self.current_state.run_command)

        log.debug("Asking for new feature")

        feature, user_desc = None, None

        while True:
            response = await self.ask_question(
                "Do you want to add a new feature or implement something quickly?",
                buttons={
                    # "feature": "Feature",
                    "task": "Implement new feature",
                    # "end": "No, I'm done",
                },
                buttons_only=True,
            )

            if response.button == "end" or response.cancelled:
                await self.ui.send_message("Thank you for using Pythagora!", source=pythagora_source)
                return AgentResponse.exit(self)

            if not response.text:
                feature = response.button == "feature"

                response = await self.ask_question(
                    "What do you want to implement?",
                    buttons={"back": "Back"},
                    allow_empty=False,
                )

            if response.text:
                user_desc = response.text
                break

        if feature:
            await self.ui.send_project_stage(
                {
                    "stage": ProjectStage.STARTING_NEW_FEATURE,
                    "feature_number": len(self.current_state.epics),
                }
            )
            self.next_state.epics = self.current_state.epics + [
                {
                    "id": uuid4().hex,
                    "name": f"Feature #{len(self.current_state.epics)}",
                    "test_instructions": None,
                    "source": "feature",
                    "description": user_desc,
                    "summary": None,
                    "completed": False,
                    "complexity": None,  # Determined and defined in SpecWriter
                    "sub_epics": [],
                }
            ]
            # Orchestrator will rerun us to break down the new feature epic
            self.next_state.action = TL_START_FEATURE.format(len(self.current_state.epics))
            return AgentResponse.update_specification(self, user_desc)
        else:
            # Quick implementation
            # TODO send project stage?

            # load the previous state, because in this state we have deleted tasks due to epic being completed!
            wanted_project_state = await self.state_manager.get_project_state_by_id(self.current_state.prev_state_id)

            wanted_project_state.epics[-1]["completed"] = False
            self.next_state.epics = wanted_project_state.epics

            # Trim logs from existing tasks before adding the new task
            if wanted_project_state.tasks:
                # Trim logs from all existing tasks
                for task in wanted_project_state.tasks:
                    if task.get("description"):
                        task["description"] = trim_logs(task["description"])

            # Create tasks list with new task (after trimming logs from existing tasks)
            self.next_state.tasks = wanted_project_state.tasks + [
                {
                    "id": uuid4().hex,
                    "description": user_desc,
                    "instructions": None,
                    "pre_breakdown_testing_instructions": None,
                    "status": TaskStatus.TODO,
                    "sub_epic_id": self.next_state.epics[-1]["sub_epics"][-1]["id"],
                    "quick_implementation": True,
                }
            ]

            # Flag tasks as modified so SQLAlchemy knows to save the changes
            self.next_state.flag_epics_as_modified()
            self.next_state.flag_tasks_as_modified()

            await self.ui.send_epics_and_tasks(
                self.next_state.epics[-1].get("sub_epics", []),
                self.next_state.tasks,
            )

            return AgentResponse.done(self)

    async def process_epic(self, sub_epic_number, sub_epic):
        epic_convo = (
            AgentConvo(self)
            .template(
                "epic_breakdown",
                epic_number=sub_epic_number,
                epic_description=sub_epic.description,
                get_only_api_files=True,
            )
            .require_schema(EpicPlan)
        )

        llm = self.get_llm(TECH_LEAD_EPIC_BREAKDOWN)
        epic_plan: EpicPlan = await llm(epic_convo, parser=JSONParser(EpicPlan))

        task = {
            "id": uuid4().hex,
            "description": "",
            "instructions": None,
            "pre_breakdown_testing_instructions": "",
            "status": TaskStatus.TODO,
            "sub_epic_id": sub_epic_number,
            "related_api_endpoints": [],
        }

        for epic_task in epic_plan.plan:
            task["description"] += (
                epic_task.description + " " if epic_task.description.endswith(".") else epic_task.description + ". "
            )
            task["related_api_endpoints"] += [rae.model_dump() for rae in (epic_task.related_api_endpoints or [])]
            task["pre_breakdown_testing_instructions"] += f"{epic_task.description}\n{epic_task.testing_instructions}\n"

        return task

    async def plan_epic(self, epic) -> AgentResponse:
        self.next_state.action = TL_CREATE_PLAN.format(epic["name"])
        log.debug(f"Planning tasks for the epic: {epic['name']}")
        await self.send_message("Creating the development plan ...")

        if epic.get("source") == "feature":
            await self.get_relevant_files_parallel(user_feedback=epic.get("description"))

        llm = self.get_llm(TECH_LEAD_PLANNING)
        convo = (
            AgentConvo(self)
            .template(
                "plan",
                epic=epic,
                task_type=self.current_state.current_epic.get("source", "app"),
                # FIXME: we're injecting summaries to initial description
                existing_summary=None,
                get_only_api_files=True,
            )
            .require_schema(DevelopmentPlan)
        )

        response: DevelopmentPlan = await llm(convo, parser=JSONParser(DevelopmentPlan))

        convo.remove_last_x_messages(1)

        await self.send_message("Creating tasks ...")
        if epic.get("source") == "feature" or epic.get("complexity") == Complexity.SIMPLE:
            self.next_state.current_epic["sub_epics"] = [
                {
                    "id": 1,
                    "description": epic["name"],
                }
            ]
        else:
            self.next_state.current_epic["sub_epics"] = [
                {
                    "id": sub_epic_number,
                    "description": sub_epic.description,
                }
                for sub_epic_number, sub_epic in enumerate(response.plan, start=1)
            ]

        # Create and gather all epic processing tasks
        epic_tasks = []
        for sub_epic_number, sub_epic in enumerate(response.plan, start=1):
            epic_tasks.append(self.process_epic(sub_epic_number, sub_epic))

        all_tasks_results = await asyncio.gather(*epic_tasks)

        for tasks_result in all_tasks_results:
            self.next_state.tasks.append(tasks_result)

        await self.ui.send_epics_and_tasks(
            self.next_state.current_epic["sub_epics"],
            self.next_state.tasks,
        )

        await self.update_epics_and_tasks()

        await self.ui.send_epics_and_tasks(
            self.next_state.current_epic["sub_epics"],
            self.next_state.tasks,
        )

        await telemetry.trace_code_event(
            "development-plan",
            {
                "num_tasks": len(self.current_state.tasks),
                "num_epics": len(self.current_state.epics),
            },
        )
        return AgentResponse.done(self)

    # TODO - Move to a separate agent for removing mocked data
    async def remove_mocked_data(self):
        files = self.current_state.files
        for file in files:
            file_content = file.content.content
            if "pythagora_mocked_data" in file_content:
                for line in file_content.split("\n"):
                    if "pythagora_mocked_data" in line:
                        file_content = file_content.replace(line + "\n", "")
                await self.state_manager.save_file(file.path, file_content)

    async def update_epics_and_tasks(self):
        if (
            self.current_state.current_epic
            and self.current_state.current_epic.get("source", "") == "app"
            and self.current_state.knowledge_base.user_options.get("auth", False)
        ):
            log.debug("Adding auth task to the beginning of the task list")
            self.next_state.tasks.insert(
                0,
                {
                    "id": uuid4().hex,
                    "hardcoded": True,
                    "description": "Implement and test Login and Register pages",
                    "instructions": """Open /register page, add your data and click on the "Register" button\nExpected result: You should see a success message in the bottom right corner and you should be redirected to the /login page\n2. On the /login page, add your data and click on the "Login" button\nExpected result: You should see a success message in the bottom right corner and you should be redirected to the home page""",
                    "test_instructions": """[
  {
    "title": "Open Register Page",
    "action": "Open your web browser and visit 'http://localhost:5173/register'.",
    "result": "You should see a success message in the bottom right corner and you should be redirected to the /login page"
  },
  {
    "title": "Open Login Page",
    "action": "Open your web browser and visit 'http://localhost:5173/login'.",
    "result": "You should see a success message in the bottom right corner and you should be redirected to the home page"
  }
]""",
                    "pre_breakdown_testing_instructions": """Open /register page, add your data and click on the "Register" button\nExpected result: You should see a success message in the bottom right corner and you should be redirected to the /login page\n2. On the /login page, add your data and click on the "Login" button\nExpected result: You should see a success message in the bottom right corner and you should be redirected to the home page""",
                    "status": TaskStatus.TODO,
                    "sub_epic_id": 1,
                    "related_api_endpoints": [
                        {
                            "description": "Register a new user",
                            "method": "POST",
                            "endpoint": "/api/auth/register",
                            "request_body": {"email": "string", "password": "string"},
                            "response_body": {
                                "id": "integer",
                                "email": "string",
                            },
                        },
                        {
                            "description": "Login user",
                            "method": "POST",
                            "endpoint": "/api/auth/login",
                            "request_body": {"username": "string", "password": "string"},
                            "response_body": {"token": "string"},
                        },
                    ],
                },
            )

            self.next_state.steps = [
                {
                    "completed": True,
                    "iteration_index": 0,
                }
            ]
        self.next_state.flag_tasks_as_modified()
        self.next_state.flag_epics_as_modified()

        await self.ui.clear_main_logs()
        await self.ui.send_project_stage(
            {
                "stage": ProjectStage.STARTING_TASK,
                "task_index": 0,
            }
        )
        await self.ui.send_front_logs_headers(
            str(self.current_state.id),
            ["E3 / T1", "Backend", "working"],
            self.next_state.tasks[0]["description"],
            self.next_state.tasks[0]["id"],
        )

        await self.ui.send_back_logs(
            [
                {
                    "title": self.next_state.tasks[0]["description"],
                    "project_state_id": str(self.next_state.id),
                    "labels": ["E3 / T1", "Backend", "working"],
                }
            ]
        )
