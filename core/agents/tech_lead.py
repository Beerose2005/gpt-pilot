from uuid import uuid4

from pydantic import BaseModel, Field

from core.agents.base import BaseAgent
from core.agents.convo import AgentConvo
from core.agents.response import AgentResponse
from core.config import TECH_LEAD_PLANNING
from core.db.models.project_state import TaskStatus
from core.llm.parser import JSONParser
from core.log import get_logger
from core.telemetry import telemetry
from core.templates.example_project import EXAMPLE_PROJECTS
from core.templates.registry import PROJECT_TEMPLATES
from core.ui.base import ProjectStage, success_source

log = get_logger(__name__)


class Epic(BaseModel):
    description: str = Field(description=("Description of an epic."))


class Task(BaseModel):
    description: str = Field(description="Description of a task.")
    testing_instructions: str = Field(description="Instructions for testing the task.")


class DevelopmentPlan(BaseModel):
    plan: list[Epic] = Field(description="List of epics that need to be done to implement the entire plan.")


class EpicPlan(BaseModel):
    plan: list[Task] = Field(description="List of tasks that need to be done to implement the entire epic.")


class TechLead(BaseAgent):
    agent_type = "tech-lead"
    display_name = "Tech Lead"

    async def run(self) -> AgentResponse:
        if len(self.current_state.epics) == 0:
            if self.current_state.specification.example_project:
                self.plan_example_project()
            else:
                self.create_initial_project_epic()
            return AgentResponse.done(self)

        await self.ui.send_project_stage(ProjectStage.CODING)

        if self.current_state.specification.templates and not self.current_state.files:
            await self.apply_project_templates()
            self.next_state.action = "Apply project templates"
            await self.ui.send_epics_and_tasks(
                self.next_state.current_epic["sub_epics"],
                self.next_state.tasks,
            )

            inputs = []
            for file in self.next_state.files:
                input_required = self.state_manager.get_input_required(file.content.content)
                if input_required:
                    inputs += [{"file": file.path, "line": line} for line in input_required]

            if inputs:
                return AgentResponse.input_required(self, inputs)
            else:
                return AgentResponse.done(self)

        if self.current_state.current_epic:
            self.next_state.action = "Create a development plan"
            return await self.plan_epic(self.current_state.current_epic)
        else:
            return await self.ask_for_new_feature()

    def create_initial_project_epic(self):
        log.debug("Creating initial project Epic")
        self.next_state.epics = [
            {
                "id": uuid4().hex,
                "name": "Initial Project",
                "source": "app",
                "description": self.current_state.specification.description,
                "test_instructions": None,
                "summary": None,
                "completed": False,
                "complexity": self.current_state.specification.complexity,
                "sub_epics": [],
            }
        ]

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
        else:
            await self.ui.send_message("Your app is DONE! You can start using it right now!", source=success_source)

        if self.current_state.run_command:
            await self.ui.send_run_command(self.current_state.run_command)

        log.debug("Asking for new feature")
        response = await self.ask_question(
            "Do you have a new feature to add to the project? Just write it here:",
            buttons={"continue": "continue", "end": "No, I'm done"},
            allow_empty=False,
        )

        if response.button == "end" or response.cancelled or not response.text:
            await self.ui.send_message("Thanks for using Pythagora!")
            return AgentResponse.exit(self)

        self.next_state.epics = self.current_state.epics + [
            {
                "id": uuid4().hex,
                "name": f"Feature #{len(self.current_state.epics)}",
                "test_instructions": None,
                "source": "feature",
                "description": response.text,
                "summary": None,
                "completed": False,
                "complexity": None,  # Determined and defined in SpecWriter
                "sub_epics": [],
            }
        ]
        # Orchestrator will rerun us to break down the new feature epic
        self.next_state.action = f"Start of feature #{len(self.current_state.epics)}"
        return AgentResponse.update_specification(self, response.text)

    async def plan_epic(self, epic) -> AgentResponse:
        log.debug(f"Planning tasks for the epic: {epic['name']}")
        await self.send_message("Creating the development plan ...")

        llm = self.get_llm(TECH_LEAD_PLANNING)
        convo = (
            AgentConvo(self)
            .template(
                "plan",
                epic=epic,
                task_type=self.current_state.current_epic.get("source", "app"),
                # FIXME: we're injecting summaries to initial description
                existing_summary=None,
            )
            .require_schema(DevelopmentPlan)
        )

        response: DevelopmentPlan = await llm(convo, parser=JSONParser(DevelopmentPlan))

        convo.remove_last_x_messages(1)
        formatted_tasks = [f"Epic #{index}: {task.description}" for index, task in enumerate(response.plan, start=1)]
        tasks_string = "\n\n".join(formatted_tasks)
        convo = convo.assistant(tasks_string)
        llm = self.get_llm(TECH_LEAD_PLANNING)

        if epic.get("source") == "feature" or epic.get("complexity") == "simple":
            await self.send_message(f"Epic 1: {epic['name']}")
            self.next_state.current_epic["sub_epics"] = [
                {
                    "id": 1,
                    "description": epic["name"],
                }
            ]
            await self.send_message("Creating tasks for this epic ...")
            self.next_state.tasks = self.next_state.tasks + [
                {
                    "id": uuid4().hex,
                    "description": task.description,
                    "instructions": None,
                    "pre_breakdown_testing_instructions": None,
                    "status": TaskStatus.TODO,
                    "sub_epic_id": 1,
                }
                for task in response.plan
            ]
            await self.ui.send_epics_and_tasks(
                self.next_state.current_epic["sub_epics"],
                self.next_state.tasks,
            )
        else:
            self.next_state.current_epic["sub_epics"] = self.next_state.current_epic["sub_epics"] + [
                {
                    "id": sub_epic_number,
                    "description": sub_epic.description,
                }
                for sub_epic_number, sub_epic in enumerate(response.plan, start=1)
            ]
            for sub_epic_number, sub_epic in enumerate(response.plan, start=1):
                await self.send_message(f"Epic {sub_epic_number}: {sub_epic.description}")
                convo = convo.template(
                    "epic_breakdown", epic_number=sub_epic_number, epic_description=sub_epic.description
                ).require_schema(EpicPlan)
                await self.send_message("Creating tasks for this epic ...")
                epic_plan: EpicPlan = await llm(convo, parser=JSONParser(EpicPlan))
                self.next_state.tasks = self.next_state.tasks + [
                    {
                        "id": uuid4().hex,
                        "description": task.description,
                        "instructions": None,
                        "pre_breakdown_testing_instructions": task.testing_instructions,
                        "status": TaskStatus.TODO,
                        "sub_epic_id": sub_epic_number,
                    }
                    for task in epic_plan.plan
                ]
                convo.remove_last_x_messages(2)

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

    def plan_example_project(self):
        example_name = self.current_state.specification.example_project
        log.debug(f"Planning example project: {example_name}")

        example = EXAMPLE_PROJECTS[example_name]
        self.next_state.epics = [
            {
                "name": "Initial Project",
                "description": example["description"],
                "completed": False,
                "complexity": example["complexity"],
                "sub_epics": [
                    {
                        "id": 1,
                        "description": "Single Epic Example",
                    }
                ],
            }
        ]
        self.next_state.tasks = example["plan"]
