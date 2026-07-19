"""Deterministic LLM stand-in for the v1.4 e2e suite.

When ``BSS_LLM_FIXTURE_PATH`` is set, :func:`build_chat_model` returns an
instance of :class:`MockChatModel` instead of a real ``ChatOpenAI`` client.
The mock answers from a JSON fixture file keyed by substring match against
the user's latest message, so cockpit specs can assert on tool-call shape
+ final rendering without burning OpenRouter quota or flaking on output.

**Fixture file format** (``packages/bss-e2e/fixtures/cockpit_e2e.json``):

.. code-block:: json

    {
      "responses": [
        {
          "name": "tool-roundtrip-customer-list",
          "match": "list customers",
          "steps": [
            {
              "tool_calls": [
                {
                  "name": "customer.list",
                  "args": {"name_contains": "Demo"}
                }
              ]
            },
            {"content": "Here are the customers matching 'Demo'."}
          ]
        }
      ]
    }

Each response has:
- ``match`` — substring matched (case-insensitive) against the *latest* user
  message in the conversation.
- ``steps`` — one entry per LLM turn in the ReAct loop. Step 0 is the first
  ``_agenerate()`` call; if it carries ``tool_calls`` the agent executes
  them and calls back into the model with the tool result, which we answer
  from step 1; etc. The last step is typically a plain ``content`` reply.

**Statefulness.** A fresh ``MockChatModel`` is instantiated per turn (because
``build_chat_model()`` is called inside ``build_graph()`` which builds anew
per ``astream_once`` invocation). The mock tracks its step pointer in a
private attribute so consecutive ``_agenerate`` calls within the same turn
walk the steps array. Across turns / sessions the pointer resets.

**LangChain shape.** Inherits ``BaseChatModel`` so LangGraph's
``create_react_agent`` accepts it (it expects a Runnable; the duck-typed
``ainvoke`` shim alone is insufficient — LangGraph wraps the model in a
``RunnableBinding``).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

log = logging.getLogger(__name__)


class MockChatModel(BaseChatModel):
    """A LangChain ``BaseChatModel`` that reads responses from a JSON
    fixture. Used only when ``BSS_LLM_FIXTURE_PATH`` is set — the
    cockpit e2e specs rely on it to assert on tool-call rendering
    without flaking on real model output."""

    fixture_path: str
    """Absolute path to the fixture JSON file. Public so Pydantic
    accepts it as a constructor arg."""

    _fixtures: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _call_count: int = PrivateAttr(default=0)
    _matched_response: dict[str, Any] | None = PrivateAttr(default=None)

    def __init__(self, fixture_path: str, **kwargs: Any) -> None:
        super().__init__(fixture_path=fixture_path, **kwargs)
        self._fixtures = self._load_fixtures(Path(fixture_path))

    @staticmethod
    def _load_fixtures(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            raise RuntimeError(
                f"BSS_LLM_FIXTURE_PATH={path} does not exist or is not a file. "
                "Bind-mount the fixture into the container or unset the env."
            )
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        responses = data.get("responses", [])
        if not isinstance(responses, list):
            raise RuntimeError(
                f"fixture {path}: 'responses' must be a list, got {type(responses)}"
            )
        for r in responses:
            if "match" not in r or "steps" not in r:
                raise RuntimeError(
                    f"fixture {path}: response missing 'match' or 'steps' "
                    f"keys: {r!r}"
                )
        return responses

    # ── BaseChatModel surface ─────────────────────────────────────────────

    @property
    def _llm_type(self) -> str:
        return "bss-mock-chat-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = self._next_message(messages)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = self._next_message(messages)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> "MockChatModel":
        """LangGraph's ReAct agent calls bind_tools to register tool schemas.
        The mock ignores the schemas (fixture supplies tool_calls verbatim)
        but must return a chat-model-shaped object to satisfy the wiring."""
        return self

    # ── matching + step walk ─────────────────────────────────────────────

    def _next_message(self, messages: list[BaseMessage]) -> AIMessage:
        if self._matched_response is None:
            user_text = _latest_user_text(messages)
            self._matched_response = self._match(user_text)
            log.info(
                "mock_llm.matched fixture=%s matched=%s user_preview=%r",
                (self._matched_response or {}).get("name"),
                self._matched_response is not None,
                user_text[:80],
            )

        steps = (self._matched_response or {}).get("steps") or []
        if self._call_count >= len(steps):
            # Out of scripted turns — return a neutral "done" reply so the
            # ReAct loop breaks cleanly rather than recursing forever.
            self._call_count += 1
            return AIMessage(content="(done)")

        step = steps[self._call_count]
        self._call_count += 1

        raw_calls = step.get("tool_calls") or []
        tool_calls: list[dict[str, Any]] = []
        for i, tc in enumerate(raw_calls):
            tool_calls.append(
                {
                    "id": tc.get("id", f"mock_call_{self._call_count}_{i}"),
                    "name": tc["name"],
                    "args": tc.get("args", {}),
                    "type": "tool_call",
                }
            )

        return AIMessage(
            content=step.get("content", ""),
            tool_calls=tool_calls,
        )

    def _match(self, user_text: str) -> dict[str, Any] | None:
        if not user_text:
            return None
        text_lower = user_text.lower()
        for response in self._fixtures:
            needle = response.get("match", "")
            if needle and needle.lower() in text_lower:
                return response
        return None


def _latest_user_text(messages: list[BaseMessage]) -> str:
    """The latest HumanMessage's content (str, joined if multi-part)."""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
    return ""


def build_mock_chat_model() -> MockChatModel | None:
    """Return a :class:`MockChatModel` if ``BSS_LLM_FIXTURE_PATH`` is set
    and readable; otherwise ``None``. ``build_chat_model`` calls this
    first and falls back to the real OpenRouter client when it returns
    ``None``."""
    path_str = os.environ.get("BSS_LLM_FIXTURE_PATH", "").strip()
    if not path_str:
        return None
    return MockChatModel(fixture_path=path_str)
