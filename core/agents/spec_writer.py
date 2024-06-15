from core.agents.base import BaseAgent
from core.agents.convo import AgentConvo
from core.agents.response import AgentResponse
from core.db.models import Complexity
from core.llm.parser import StringParser
from core.log import get_logger
from core.telemetry import telemetry
from core.templates.example_project import (
    DEFAULT_EXAMPLE_PROJECT,
    EXAMPLE_PROJECTS,
)

# If the project description is less than this, perform an analysis using LLM
ANALYZE_THRESHOLD = 1500
# URL to the wiki page with tips on how to write a good project description
INITIAL_PROJECT_HOWTO_URL = (
    "https://github.com/Pythagora-io/gpt-pilot/wiki/How-to-write-a-good-initial-project-description"
)
SPEC_STEP_NAME = "Create specification"

log = get_logger(__name__)


class SpecWriter(BaseAgent):
    agent_type = "spec-writer"
    display_name = "Spec Writer"

    async def run(self) -> AgentResponse:
        response = await self.ask_question(
            "Describe your app in as much detail as possible",
            allow_empty=False,
            buttons={
                # FIXME: must be lowercase becase VSCode doesn't recognize it otherwise. Needs a fix in the extension
                "continue": "continue",
                "example": "Start an example project",
                "import": "Import an existing project",
            },
        )
        if response.cancelled:
            return AgentResponse.error(self, "No project description")

        if response.button == "import":
            return AgentResponse.import_project(self)

        if response.button == "example":
            await self.prepare_example_project(DEFAULT_EXAMPLE_PROJECT)
            return AgentResponse.done(self)

        elif response.button == "continue":
            # FIXME: Workaround for the fact that VSCode "continue" button does
            # nothing but repeat the question. We reproduce this bug for bug here.
            return AgentResponse.done(self)

        spec = response.text.strip()

        complexity = await self.check_prompt_complexity(spec)
        await telemetry.trace_code_event(
            "project-description",
            {
                "initial_prompt": spec,
                "complexity": complexity,
            },
        )

        if len(spec) < ANALYZE_THRESHOLD and complexity != Complexity.SIMPLE:
            spec = await self.analyze_spec(spec)
            spec = await self.review_spec(spec)

        self.next_state.specification = self.current_state.specification.clone()
        self.next_state.specification.description = spec
        self.next_state.specification.complexity = complexity
        telemetry.set("initial_prompt", spec)
        telemetry.set("is_complex_app", complexity != Complexity.SIMPLE)

        self.next_state.action = SPEC_STEP_NAME
        return AgentResponse.done(self)

    async def check_prompt_complexity(self, prompt: str) -> str:
        await self.send_message("Checking the complexity of the prompt ...")
        llm = self.get_llm()
        convo = AgentConvo(self).template("prompt_complexity", prompt=prompt)
        llm_response: str = await llm(convo, temperature=0, parser=StringParser())
        return llm_response.lower()

    async def prepare_example_project(self, example_name: str):
        example_description = EXAMPLE_PROJECTS[example_name]["description"].strip()

        log.debug(f"Starting example project: {example_name}")
        await self.send_message(f"Starting example project with description:\n\n{example_description}")

        spec = self.current_state.specification.clone()
        spec.example_project = example_name
        spec.description = example_description
        spec.complexity = EXAMPLE_PROJECTS[example_name]["complexity"]
        self.next_state.specification = spec

        telemetry.set("initial_prompt", spec.description)
        telemetry.set("example_project", example_name)
        telemetry.set("is_complex_app", spec.complexity != Complexity.SIMPLE)

    async def analyze_spec(self, spec: str) -> str:
        msg = (
            "Your project description seems a bit short. "
            "The better you can describe the project, the better GPT Pilot will understand what you'd like to build.\n\n"
            f"Here are some tips on how to better describe the project: {INITIAL_PROJECT_HOWTO_URL}\n\n"
            "Let's start by refining your project idea:"
        )
        await self.send_message(msg)

        llm = self.get_llm()
        convo = AgentConvo(self).template("ask_questions").user(spec)
        n_questions = 0
        n_answers = 0

        while True:
            response: str = await llm(convo)
            if len(response) > 500:
                # The response is too long for it to be a question, assume it's the spec
                confirm = await self.ask_question(
                    (
                        "Can we proceed with this project description? If so, just press ENTER. "
                        "Otherwise, please tell me what's missing or what you'd like to add."
                    ),
                    allow_empty=True,
                    buttons={"continue": "continue"},
                )
                if confirm.cancelled or confirm.button == "continue" or confirm.text == "":
                    await telemetry.trace_code_event(
                        "spec-writer-questions",
                        {
                            "num_questions": n_questions,
                            "num_answers": n_answers,
                            "new_spec": spec,
                        },
                    )
                    return spec
                convo.user(confirm.text)

            else:
                convo.assistant(response)

                n_questions += 1
                user_response = await self.ask_question(
                    response,
                    buttons={"skip": "Skip questions"},
                )
                if user_response.cancelled or user_response.button == "skip":
                    convo.user(
                        "This is enough clarification, you have all the information. "
                        "Please output the spec now, without additional comments or questions."
                    )
                    response: str = await llm(convo)
                    return response

                n_answers += 1
                convo.user(user_response.text)

    async def review_spec(self, spec: str) -> str:
        convo = AgentConvo(self).template("review_spec", spec=spec)
        llm = self.get_llm()
        llm_response: str = await llm(convo, temperature=0)
        additional_info = llm_response.strip()
        if additional_info:
            spec += "\nAdditional info/examples:\n" + additional_info
        return spec
