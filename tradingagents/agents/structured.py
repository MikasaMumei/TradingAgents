"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema, include_raw=True)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. Render a successful parse; on a parse miss retain the original visible
   answer. Only call the plain model when no original text is available, and
   log that additional call as a new decision with an unknown original rating.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Schema-only structured output binds exactly one tool (the schema itself), so a
# model that reaches for a search tool emits an unknown tool call and the whole
# structured attempt is discarded for a free-text retry. Agents on this path
# state the constraint explicitly rather than relying on the binding alone
# (#1130).
NO_EXTERNAL_TOOLS = (
    "Use only the evidence provided in this prompt. Do not call external tools "
    "or search the web; if something is missing, say so explicitly."
)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """Bind structured output with its raw reply, or use one plain call if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema, include_raw=True)
    except (TypeError, ValueError) as exc:
        # Older adapters may not expose include_raw. Binding without it cannot
        # preserve a parse miss, so use one plain call rather than two decisions.
        if "include_raw" not in str(exc):
            raise
        logger.warning("%s: raw structured output unsupported; using free text", agent_name)
        return None
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name,
            exc,
        )
        return None


@dataclass(frozen=True)
class StructuredReply:
    """The parsed value and the original message from one model invocation."""

    parsed: Any = None
    raw: Any = None


def invoke_structured_reply(
    structured_llm: Any | None, prompt: Any, agent_name: str
) -> StructuredReply:
    if structured_llm is None:
        return StructuredReply()
    try:
        result = structured_llm.invoke(prompt)
    except Exception as exc:
        logger.warning("%s: structured invocation failed (%s)", agent_name, type(exc).__name__)
        return StructuredReply(raw=getattr(exc, "response", None))
    if isinstance(result, dict) and {"raw", "parsed", "parsing_error"} <= result.keys():
        if result["parsed"] is None:
            logger.warning("%s: structured parse miss; original reply retained", agent_name)
        return StructuredReply(parsed=result["parsed"], raw=result["raw"])
    # Retain compatibility for callers passing an already parsed runnable.
    return StructuredReply(parsed=result)


def invoke_structured(structured_llm: Any | None, prompt: Any, agent_name: str) -> T | None:
    """Run the structured call; ``None`` when there is none or it fails.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape), so a caller can forward the same value to its free-text fallback.
    """
    return invoke_structured_reply(structured_llm, prompt, agent_name).parsed


def reply_text(message: Any) -> str:
    """Read visible text only; never turn tool arguments or reasoning into prose."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in ("text", "output_text")
        )
    return ""


def freetext_for_reply(
    reply: StructuredReply,
    plain_llm: Any,
    prompt: Any,
    agent_name: str,
    *,
    attempted: bool,
) -> str:
    text = reply_text(reply.raw)
    if text.strip():
        logger.info("%s: using original text after parse miss; no second model call", agent_name)
        return text
    if attempted:
        logger.warning(
            "%s: no usable original text; free-text call is a new decision, "
            "original rating unknown",
            agent_name,
        )
    return reply_text(plain_llm.invoke(prompt))


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure."""
    reply = invoke_structured_reply(structured_llm, prompt, agent_name)
    if reply.parsed is not None:
        return render(reply.parsed)
    return freetext_for_reply(
        reply,
        plain_llm,
        prompt,
        agent_name,
        attempted=structured_llm is not None,
    )
