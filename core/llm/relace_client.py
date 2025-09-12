from typing import Optional

import httpx
from httpx import AsyncClient

from core.config import LLMProvider
from core.llm.base import BaseLLMClient
from core.llm.convo import Convo
from core.log import get_logger

log = get_logger(__name__)


class RelaceClient(BaseLLMClient):
    provider = LLMProvider.RELACE

    def _init_client(self):
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.state_manager.get_access_token() if self.state_manager.get_access_token() is not None else self.config.api_key if self.config.api_key is not None else ''}",
        }
        self.client = AsyncClient()

    async def _make_request(
        self,
        convo: Convo,
        temperature: Optional[float] = None,
        json_mode: bool = False,
    ) -> tuple[str, int, int]:
        """
        Make a POST request to the Relace API to merge code snippets.

        :param convo: Conversation object containing initial code and edit snippet.
        :param temperature: Not used in this implementation.
        :param json_mode: Not used in this implementation.
        :return: Merged code, input tokens (0), and output tokens (0).
        """
        data = {
            "initialCode": convo.messages[0]["content"]["initialCode"],
            "editSnippet": convo.messages[0]["content"]["editSnippet"],
            "model": self.config.model,
        }

        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport()) as client:
            try:
                response = await client.post(
                    "https://api.pythagora.io/v1/relace/merge", headers=self.headers, json=data
                )
                response.raise_for_status()
                response_json = response.json()
                return (
                    response_json.get("content", ""),
                    response_json.get("inputTokens", 0),
                    response_json.get("outputTokens", 0),
                )
            except Exception as e:
                # Fall back to other ai provider
                log.debug(f"Relace API request failed: {e}")
                return ("", 0, 0)


__all__ = ["RelaceClient"]
