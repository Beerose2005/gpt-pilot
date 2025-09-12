import secrets

from core.agents.base import BaseAgent
from core.agents.convo import AgentConvo
from core.agents.response import AgentResponse, ResponseType
from core.config import DEFAULT_AGENT_NAME, SPEC_WRITER_AGENT_NAME
from core.config.actions import SPEC_CHANGE_FEATURE_STEP_NAME, SPEC_CHANGE_STEP_NAME, SPEC_CREATE_STEP_NAME
from core.db.models import Complexity
from core.db.models.project_state import IterationStatus
from core.llm.parser import StringParser
from core.log import get_logger
from core.telemetry import telemetry
from core.templates.registry import PROJECT_TEMPLATES
from core.ui.base import ProjectStage

log = get_logger(__name__)


class SpecWriter(BaseAgent):
    agent_type = "spec-writer"
    display_name = "Spec Writer"

    async def run(self) -> AgentResponse:
        current_iteration = self.current_state.current_iteration
        if current_iteration is not None and current_iteration.get("status") == IterationStatus.NEW_FEATURE_REQUESTED:
            return await self.update_spec(iteration_mode=True)
        elif self.prev_response and self.prev_response.type == ResponseType.UPDATE_SPECIFICATION:
            return await self.update_spec(iteration_mode=False)
        elif not self.current_state.specification.description:
            return await self.initialize_spec_and_project()
        else:
            return await self.change_spec()

    async def apply_template(self):
        """
        Applies a template to the frontend.
        """
        options = self.current_state.knowledge_base.user_options
        if options["auth_type"] == "api_key" or options["auth_type"] == "none":
            template_name = "vite_react_swagger"
        else:
            template_name = "vite_react"
        template_class = PROJECT_TEMPLATES.get(template_name)
        if not template_class:
            log.error(f"Project template not found: {template_name}")
            return

        template = template_class(
            options,
            self.state_manager,
            self.process_manager,
        )
        if not self.state_manager.template:
            self.state_manager.template = {}
        self.state_manager.template["template"] = template
        log.info(f"Applying project template: {template.name}")
        summary = await template.apply()

        self.next_state.relevant_files = template.relevant_files
        self.next_state.modified_files = {}
        self.next_state.specification.template_summary = summary

    async def initialize_spec_and_project(self) -> AgentResponse:
        self.next_state.action = SPEC_CREATE_STEP_NAME

        await self.ui.send_project_stage({"stage": ProjectStage.PROJECT_DESCRIPTION})

        await self.ui.clear_main_logs()

        # Check if initial_prompt is provided in command line arguments
        if self.args and self.args.initial_prompt:
            description = self.args.initial_prompt.strip()
            await self.ui.send_back_logs(
                [
                    {
                        "title": "",
                        "project_state_id": "spec",
                        "labels": [""],
                        "convo": [
                            {"role": "assistant", "content": "Please describe the app you want to build."},
                            {"role": "user", "content": description},
                        ],
                    }
                ]
            )
        else:
            user_description = await self.ask_question(
                "Please describe the app you want to build.",
                allow_empty=False,
                full_screen=True,
                verbose=True,
                extra_info={
                    "chat_section_tip": "\"Some text <a href='https://example.com'>link text</a> on how to build apps with Pythagora.\""
                },
            )
            description = user_description.text.strip()

            await self.ui.send_back_logs(
                [
                    {
                        "title": "",
                        "project_state_id": self.current_state.id,
                        "labels": [""],
                        "convo": [
                            {"role": "assistant", "content": "Please describe the app you want to build."},
                            {"role": "user", "content": description},
                        ],
                    }
                ]
            )

        await self.ui.send_back_logs(
            [
                {
                    "title": "Writing Specification",
                    "project_state_id": "spec",
                    "labels": ["E1 / T1", "Specs", "working"],
                    "disallow_reload": True,
                }
            ]
        )

        await self.ui.send_front_logs_headers("specs_0", ["E1 / T1", "Writing Specification", "working"], "")

        await self.send_message(
            "## Write specification\n\nPythagora is generating a detailed specification for app based on your input.",
            # project_state_id="setup",
        )

        llm = self.get_llm(SPEC_WRITER_AGENT_NAME, stream_output=True, route="forwardToCenter")
        convo = AgentConvo(self).template(
            "build_full_specification",
            initial_prompt=description,
        )

        llm_assisted_description = await llm(convo)

        await self.ui.send_project_stage({"stage": ProjectStage.PROJECT_NAME})

        llm = self.get_llm(DEFAULT_AGENT_NAME)
        convo = AgentConvo(self).template(
            "project_name",
            description=llm_assisted_description,
        )
        llm_response: str = await llm(convo, temperature=0)
        project_name = llm_response.strip()

        self.state_manager.project.name = project_name
        self.state_manager.project.folder_name = project_name.replace(" ", "_").replace("-", "_")

        self.state_manager.file_system = await self.state_manager.init_file_system(load_existing=False)

        self.process_manager.root_dir = self.state_manager.file_system.root

        self.next_state.knowledge_base.user_options["original_description"] = description
        self.next_state.knowledge_base.user_options["project_description"] = llm_assisted_description

        self.next_state.specification = self.current_state.specification.clone()
        self.next_state.specification.description = llm_assisted_description
        self.next_state.specification.original_description = description
        return AgentResponse.done(self)

    async def change_spec(self) -> AgentResponse:
        llm = self.get_llm(SPEC_WRITER_AGENT_NAME, stream_output=True, route="forwardToCenter")

        description = self.current_state.specification.original_description

        current_description = self.current_state.specification.description
        convo = AgentConvo(self).template(
            "build_full_specification",
            initial_prompt=self.current_state.specification.description.strip(),
        )

        while True:
            user_done_with_description = await self.ask_question(
                "Are you satisfied with the project description?",
                buttons={
                    "yes": "Yes",
                    "no": "No, I want to add more details",
                },
                default="yes",
                buttons_only=True,
            )

            if user_done_with_description.button == "yes":
                await self.ui.send_project_stage({"stage": ProjectStage.SPECS_FINISHED})
                break
            elif user_done_with_description.button == "no":
                await self.send_message("## What would you like to add?")
                user_add_to_spec = await self.ask_question(
                    "What would you like to add?",
                    allow_empty=False,
                )
            else:
                user_add_to_spec = user_done_with_description

            await self.send_message("## Refining specification\n\nPythagora is refining the specs based on your input.")
            # if user edits the spec with extension, it will be commited to db immediately, so we have to check if the description has changed
            if current_description != self.current_state.specification.description:
                convo = AgentConvo(self).template(
                    "build_full_specification",
                    initial_prompt=self.current_state.specification.description.strip(),
                )

            convo = convo.template("add_to_specification", user_message=user_add_to_spec.text.strip())

            if len(convo.messages) > 6:
                convo.slice(1, 4)

            # await self.ui.set_important_stream()
            llm_assisted_description = await llm(convo)

            # when llm generates a new spec - make it the new default spec, even if user edited it before - because it will be shown in the extension
            self.current_state.specification.description = llm_assisted_description
            convo = convo.assistant(llm_assisted_description)

        await self.ui.clear_main_logs()
        await self.ui.send_back_logs(
            [
                {
                    "title": "Writing Specification",
                    "project_state_id": "spec",  # self.current_state.id,
                    "labels": ["E1 / T1", "Specs", "done"],
                    "convo": [
                        {
                            "role": "assistant",
                            "content": "What do you want to build?",
                        },
                        {
                            "role": "user",
                            "content": self.current_state.specification.original_description,
                        },
                    ],
                    "disallow_reload": True,
                }
            ]
        )

        llm = self.get_llm(SPEC_WRITER_AGENT_NAME)
        convo = AgentConvo(self).template(
            "need_auth",
            description=self.current_state.specification.description,
        )
        llm_response: str = await llm(convo, temperature=0)
        auth = llm_response.strip().lower() == "yes"

        if auth:
            self.next_state.knowledge_base.user_options["auth"] = auth
            self.next_state.knowledge_base.user_options["jwt_secret"] = secrets.token_hex(32)
            self.next_state.knowledge_base.user_options["refresh_token_secret"] = secrets.token_hex(32)
            self.next_state.flag_knowledge_base_as_modified()

        # if we reload the project from the 1st project state, state_manager.template will be None
        if self.state_manager.template:
            self.state_manager.template["description"] = self.current_state.specification.description
        else:
            # if we do not set this and reload the project, we will load the "old" project description we entered before reload
            self.next_state.epics[0]["description"] = self.current_state.specification.description

        self.next_state.specification = self.current_state.specification.clone()
        self.next_state.specification.original_description = description
        self.next_state.specification.description = self.current_state.specification.description

        complexity = await self.check_prompt_complexity(self.current_state.specification.description)
        self.next_state.specification.complexity = complexity

        telemetry.set("initial_prompt", description)
        telemetry.set("updated_prompt", self.current_state.specification.description)
        telemetry.set("is_complex_app", complexity != Complexity.SIMPLE)

        await self.ui.send_project_description(
            {
                "project_description": self.current_state.specification.description,
                "project_type": self.current_state.branch.project.project_type,
            }
        )

        await telemetry.trace_code_event(
            "project-description",
            {
                "complexity": complexity,
                "initial_prompt": description,
                "llm_assisted_prompt": self.current_state.specification.description,
            },
        )

        self.next_state.epics = [
            {
                "id": self.current_state.epics[0]["id"],
                "name": "Build frontend",
                "source": "frontend",
                "description": self.current_state.specification.description,
                "messages": [],
                "summary": None,
                "completed": False,
            }
        ]

        if not self.state_manager.async_tasks:
            self.state_manager.async_tasks = []
            await self.apply_template()

        return AgentResponse.done(self)

    async def update_spec(self, iteration_mode) -> AgentResponse:
        if iteration_mode:
            self.next_state.action = SPEC_CHANGE_FEATURE_STEP_NAME
            feature_description = self.current_state.current_iteration["user_feedback"]
        else:
            self.next_state.action = SPEC_CHANGE_STEP_NAME
            feature_description = self.prev_response.data["description"]

        await self.send_message(
            f"Making the following changes to project specification:\n\n{feature_description}\n\nUpdated project specification:"
        )
        llm = self.get_llm(SPEC_WRITER_AGENT_NAME, stream_output=True, route="forwardToCenter")
        convo = AgentConvo(self).template("add_new_feature", feature_description=feature_description)
        llm_response: str = await llm(convo, temperature=0, parser=StringParser())
        updated_spec = llm_response.strip()
        await self.ui.generate_diff(
            "project_specification", self.current_state.specification.description, updated_spec, source=self.ui_source
        )
        user_response = await self.ask_question(
            "Do you accept these changes to the project specification?",
            buttons={"yes": "Yes", "no": "No"},
            default="yes",
            buttons_only=True,
        )
        await self.ui.close_diff()

        if user_response.button == "yes":
            self.next_state.specification = self.current_state.specification.clone()
            self.next_state.specification.description = updated_spec
            telemetry.set("updated_prompt", updated_spec)

        if iteration_mode:
            self.next_state.current_iteration["status"] = IterationStatus.FIND_SOLUTION
            self.next_state.flag_iterations_as_modified()
        else:
            complexity = await self.check_prompt_complexity(feature_description)
            self.next_state.current_epic["complexity"] = complexity

        return AgentResponse.done(self)

    async def check_prompt_complexity(self, prompt: str) -> str:
        is_feature = self.current_state.epics and len(self.current_state.epics) > 2
        await self.send_message("Checking the complexity of the prompt...\n")
        llm = self.get_llm(SPEC_WRITER_AGENT_NAME)
        convo = AgentConvo(self).template(
            "prompt_complexity",
            prompt=prompt,
            is_feature=is_feature,
        )
        llm_response: str = await llm(convo, temperature=0, parser=StringParser())
        log.info(f"Complexity check response: {llm_response}")
        return llm_response.lower()
