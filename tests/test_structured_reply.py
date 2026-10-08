"""Exercise the installed LangChain parser, not a mock exception.response."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.structured import bind_structured, invoke_structured_or_freetext


class OfflineChatModel(BaseChatModel):
    responses: list[AIMessage]
    calls: int = 0
    prompts: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "offline-parser-regression"

    def bind_tools(self, tools, **kwargs):
        return self.bind(**kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        self.prompts.append(messages)
        return ChatResult(generations=[ChatGeneration(message=response)])


def test_plain_first_decision_survives_real_parser_none():
    first = "**Recommendation**: Overweight\nThe original evidence supports this decision."
    model = OfflineChatModel(responses=[AIMessage(content=first)])
    plain = MagicMock()
    chain = bind_structured(model, ResearchPlan, "Research Manager")
    result = invoke_structured_or_freetext(
        chain, plain, "prompt", render_research_plan, "Research Manager"
    )
    assert result == first
    assert model.calls == 1
    plain.invoke.assert_not_called()


def test_real_validation_error_keeps_original_text():
    first = "**Recommendation**: Overweight\nAn incomplete schema must not reverse this call."
    model = OfflineChatModel(
        responses=[
            AIMessage(
                content=first,
                tool_calls=[
                    {"name": "ResearchPlan", "args": {"recommendation": "Overweight"}, "id": "bad"},
                ],
            )
        ]
    )
    plain = MagicMock()
    chain = bind_structured(model, ResearchPlan, "Research Manager")
    assert (
        invoke_structured_or_freetext(chain, plain, "p", render_research_plan, "Research Manager")
        == first
    )
    assert model.calls == 1
    plain.invoke.assert_not_called()


def test_successful_schema_is_rendered_without_plain_call():
    model = OfflineChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ResearchPlan",
                        "id": "ok",
                        "args": {
                            "recommendation": "Overweight",
                            "rationale": "Evidence",
                            "strategic_actions": "Gradual entry",
                        },
                    },
                ],
            )
        ]
    )
    plain = MagicMock()
    chain = bind_structured(model, ResearchPlan, "Research Manager")
    result = invoke_structured_or_freetext(
        chain, plain, "p", render_research_plan, "Research Manager"
    )
    assert result.startswith("**Recommendation**: Overweight")
    assert model.calls == 1
    plain.invoke.assert_not_called()


def test_unrecoverable_reply_logs_new_decision(caplog):
    model = OfflineChatModel(responses=[AIMessage(content="")])
    plain = MagicMock()
    plain.invoke.return_value = AIMessage(content="**Recommendation**: Underweight")
    chain = bind_structured(model, ResearchPlan, "Research Manager")
    result = invoke_structured_or_freetext(
        chain, plain, "p", render_research_plan, "Research Manager"
    )
    assert "Underweight" in result
    assert model.calls == 1
    plain.invoke.assert_called_once()
    assert "new decision" in caplog.text and "original rating unknown" in caplog.text


def test_research_manager_real_factory_preserves_first_reply():
    first = "**Recommendation**: Overweight\nOriginal research thesis."
    model = OfflineChatModel(responses=[AIMessage(content=first)])
    result = create_research_manager(model)(
        {
            "company_of_interest": "AAPL",
            "investment_debate_state": {
                "history": "Evidence",
                "count": 1,
            },
        }
    )
    assert result["investment_plan"] == first
    assert model.calls == 1


def test_portfolio_manager_real_factory_preserves_first_rating():
    first = "**Rating**: Overweight\nOriginal portfolio thesis."
    model = OfflineChatModel(responses=[AIMessage(content=first)])
    result = create_portfolio_manager(model)(
        {
            "company_of_interest": "AAPL",
            "investment_plan": "plan",
            "trader_investment_plan": "proposal",
            "risk_debate_state": {
                "history": "Evidence",
                "count": 1,
                **dict.fromkeys(
                    (
                        "aggressive_history",
                        "conservative_history",
                        "neutral_history",
                        "current_aggressive_response",
                        "current_conservative_response",
                        "current_neutral_response",
                    ),
                    "",
                ),
            },
        }
    )
    assert result["final_trade_decision"] == first
    assert result["final_rating"] == "Overweight"
    assert model.calls == 1


def test_adapter_without_raw_support_uses_one_plain_call():
    class OldAdapter:
        def with_structured_output(self, schema):
            raise AssertionError("Should not bind a parser that discards raw output")

    assert bind_structured(OldAdapter(), ResearchPlan, "Research Manager") is None


def test_adapter_rejecting_raw_keyword_with_value_error_uses_plain():
    class Adapter:
        def with_structured_output(self, schema, **kwargs):
            raise ValueError("include_raw is unsupported")

    assert bind_structured(Adapter(), ResearchPlan, "Research Manager") is None


def test_unrelated_binding_value_error_is_not_hidden():
    class Adapter:
        def with_structured_output(self, schema, **kwargs):
            raise ValueError("Invalid schema")

    with pytest.raises(ValueError, match="Invalid schema"):
        bind_structured(Adapter(), ResearchPlan, "Research Manager")


def test_visible_block_text_survives_without_reasoning():
    first = AIMessage(
        content=[
            {"type": "reasoning", "text": "private thought"},
            {"type": "text", "text": "**Recommendation**: Hold"},
        ]
    )
    model = OfflineChatModel(responses=[first])
    plain = MagicMock()
    chain = bind_structured(model, ResearchPlan, "Research Manager")
    assert (
        invoke_structured_or_freetext(chain, plain, "p", render_research_plan, "Research Manager")
        == "**Recommendation**: Hold"
    )
    plain.invoke.assert_not_called()


def test_probe_records_parse_miss_as_retained_text_not_success(tmp_path, monkeypatch):
    from tools_probe import probe_lib

    class ProbedModel(OfflineChatModel):
        pass

    monkeypatch.setitem(probe_lib.STATE, "log", None)
    probe_lib.install(ProbedModel)
    model = ProbedModel(responses=[AIMessage(content="**Recommendation**: Overweight")])
    log = probe_lib.set_log(tmp_path / "probe.jsonl")
    chain = bind_structured(model, ResearchPlan, "Research Manager")
    plain = MagicMock()
    result = invoke_structured_or_freetext(
        chain,
        plain,
        "p",
        render_research_plan,
        "Research Manager",
    )
    counts = probe_lib.classify(log)
    assert result == "**Recommendation**: Overweight"
    assert counts["structured_ok"] == 0
    assert counts["structured_failed"] == counts["structured_raw_retained"] == 1
    assert counts["structured_call_records"] == 1
    assert counts["calls"][0]["outcome"] == "structured_attempt"
    plain.invoke.assert_not_called()


def test_probe_exception_response_matches_native_preservation(tmp_path, monkeypatch):
    from langchain_core.runnables import RunnableLambda

    from tools_probe import probe_lib

    def failed(_):
        error = ValueError("Transport failure with visible reply")
        error.response = AIMessage(content="**Recommendation**: Overweight")
        raise error

    monkeypatch.setitem(probe_lib.STATE, "log", None)
    log = probe_lib.set_log(tmp_path / "probe.jsonl")
    plain = MagicMock()
    result = invoke_structured_or_freetext(
        probe_lib._Structured(RunnableLambda(failed), "ResearchPlan"),
        plain,
        "p",
        render_research_plan,
        "Research Manager",
    )
    assert result == "**Recommendation**: Overweight"
    assert probe_lib.classify(log)["structured_raw_retained"] == 1
    plain.invoke.assert_not_called()
