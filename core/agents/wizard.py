import json
from urllib.parse import urljoin
from uuid import uuid4

import httpx
from sqlalchemy import inspect

from core.agents.base import BaseAgent
from core.agents.response import AgentResponse
from core.cli.helpers import capture_exception
from core.config import PYTHAGORA_API
from core.config.actions import FE_INIT
from core.db.models import KnowledgeBase
from core.log import get_logger
from core.telemetry import telemetry

log = get_logger(__name__)


class Wizard(BaseAgent):
    agent_type = "wizard"
    display_name = "Wizard"

    async def run(self) -> AgentResponse:
        success = await self.init_template()
        if not success:
            return AgentResponse.exit(self)
        return AgentResponse.create_specification(self)

    async def init_template(self) -> bool:
        """
        Sets up the frontend

        :return: AgentResponse.done(self)
        """
        self.next_state.action = FE_INIT
        self.state_manager.template = {}
        options = {}
        auth_data = {}

        if self.state_manager.project.project_type == "swagger":
            while True:
                try:
                    docs = await self.ask_question(
                        "Paste the OpenAPI/Swagger JSON or YAML docs here",
                        allow_empty=False,
                        verbose=True,
                    )
                    success, options["external_api_url"], options["types"] = await self.upload_docs(docs.text)
                    if not success:
                        await self.send_message("Please try creating a new project.")
                        return False
                    else:
                        break
                except Exception as e:
                    log.debug(f"An error occurred: {str(e)}")
                    await self.send_message("Please provide a valid input.")
                    continue

            while True:
                auth_type_question = await self.ask_question(
                    "Which authentication method does your backend use?",
                    buttons={
                        "none": "No authentication",
                        "api_key": "API Key",
                        "bearer": "HTTP Bearer (coming soon)",
                        "open_id_connect": "OpenID Connect (coming soon)",
                        "oauth2": "OAuth2 (coming soon)",
                    },
                    buttons_only=True,
                    default="api_key",
                    full_screen=True,
                )

                if auth_type_question.button == "api_key":
                    if auth_data.get("types") is None or "apiKey" not in auth_data["types"]:
                        addit_question = await self.ask_question(
                            "The API key authentication method is not supported by your backend. Do you want to continue?",
                            buttons_only=True,
                            buttons={"yes": "Yes", "no": "Go back"},
                        )
                        if addit_question.button != "yes":
                            continue

                    api_key = await self.ask_question(
                        "Enter your API key here. It will be saved in the .env file on the frontend.",
                        allow_empty=False,
                        verbose=True,
                    )
                    options["auth_type"] = "api_key"
                    options["api_key"] = api_key.text.strip()
                    break
                elif auth_type_question.button == "none":
                    options["auth_type"] = "none"
                    break
                else:
                    auth_type_question_trace = await self.ask_question(
                        "We are still working on getting this auth method implemented correctly. Can we contact you to get more info on how you would like it to work?",
                        allow_empty=False,
                        buttons={"yes": "Yes", "no": "No"},
                        default="yes",
                        buttons_only=True,
                    )
                    if auth_type_question_trace.button == "yes":
                        await telemetry.trace_code_event(
                            "swagger-auth-method",
                            {"type": auth_type_question.button},
                        )
                        await self.send_message("Thank you for submitting your request. We will be in touch.")
        else:
            options["auth_type"] = "login"

        # Create a new knowledge base instance for the project state
        knowledge_base = KnowledgeBase(pages=[], apis=[], user_options=options, utility_functions=[])
        session = inspect(self.next_state).async_session
        session.add(knowledge_base)
        self.next_state.knowledge_base = knowledge_base

        self.next_state.epics = [
            {
                "id": uuid4().hex,
                "name": "Build frontend",
                "source": "frontend",
                "description": "",
                "messages": [],
                "summary": None,
                "completed": False,
            }
        ]

        return True

    async def upload_docs(self, docs: str) -> (bool, str, list):
        error = None
        url = urljoin(PYTHAGORA_API, "rag/upload")
        for attempt in range(3):
            log.debug(f"Uploading docs to RAG service... attempt {attempt}")
            try:
                async with httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(), timeout=httpx.Timeout(30.0, connect=5.0)
                ) as client:
                    resp = await client.post(
                        url,
                        json={
                            "text": docs.strip(),
                            "project_id": str(self.state_manager.project.id),
                        },
                        headers={"Authorization": f"Bearer {self.state_manager.get_access_token()}"},
                    )

                    if resp.status_code == 200:
                        log.debug("Uploading docs to RAG service successful")
                        resp_body = json.loads(resp.text)
                        return True, resp_body["external_api_url"], resp_body["types"]
                    elif resp.status_code == 403:
                        log.debug("Uploading docs to RAG service failed, trying to refresh token")
                        access_token = await self.ui.send_token_expired()
                        self.state_manager.update_access_token(access_token)
                    else:
                        try:
                            error = resp.json()["error"]
                        except Exception as e:
                            capture_exception(e)
                            error = e
                        log.debug(f"Uploading docs to RAG service failed: {error}")

            except Exception as e:
                log.warning(f"Attempt {attempt + 1} failed: {e}", exc_info=True)
                capture_exception(e)

        await self.ui.send_message(
            f"An error occurred while uploading the docs. Error: {error if error else 'unknown'}",
        )
        return False
