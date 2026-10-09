"""Regression cases for wrong accounting interpretations, not just arithmetic."""

import importlib

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from tests.test_structured_reply import OfflineChatModel
from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
from tradingagents.dataflows.financial_metrics import (
    FINANCIAL_REVIEW_MARKER,
    annotate_financial_report,
    build_summary,
    financial_claim_warnings,
)

BALANCE = "# Balance Sheet for SPCX (quarterly), USD in millions\n,2026-06-30\nCash And Cash Equivalents,93522\nCurrent Liabilities,21122\nTotal Liabilities,65546\n"
CASH = "# Cash Flow data for 600519.SS (quarterly)\n,2026-06-30,2026-03-31\nOperating Cash Flow,43780858849.93,26909891269.13\n"


def test_liabilities_cannot_supply_missing_debt():
    output = build_summary(None, None, BALANCE)
    assert "Cash minus debt: NOT AVAILABLE" in output
    assert "72,400" not in output and "27,976" not in output
    assert "Do not substitute Current Liabilities or Total Liabilities" in output


def test_explicit_debt_uses_same_date_and_does_not_replace_missing_latest_with_old_value():
    known = BALANCE + "Total Debt,12000\n"
    assert "81,522.00" in build_summary(None, None, known)
    missing = known.replace("2026-06-30", "2026-06-30,2025-12-31").replace(
        "Total Debt,12000", "Total Debt,,12000"
    )
    assert "Cash minus debt: NOT AVAILABLE" in build_summary(None, None, missing)


def test_sec_cash_row_alias_preserves_its_actual_name_and_never_substitutes_liabilities():
    text = BALANCE.replace("Cash And Cash Equivalents", "Cash and Equivalents")
    assert "Cash minus debt: NOT AVAILABLE" in build_summary(None, None, text)
    output = build_summary(None, None, text + "Total Debt,12000\n")
    assert "Cash and Equivalents minus Total Debt: 81,522.00" in output


@pytest.mark.parametrize("report,statements,code", [
    ("净现金=现金减流动负债。", {}, "net_cash_definition"),
    ("净现金头寸≈72,400美元百万（93,522−21,122）。", {"balance": BALANCE}, "net_cash_scope"),
    ("2026Q2单季经营现金流437.8亿。", {"cash": CASH}, "cash_period"),
    ("其他应收款1,440亿，为经销商预收款/打款，是需求蓄水池。", {}, "receivables_category"),
    ("FCF收益率3.8%；加上4.13%股息率，股东总回报接近8%。", {}, "return_double_count"),
    ("货币单位：CNY（数据源以人民币元计）。", {"cash": CASH}, "statement_units"),
    ("Net cash = cash minus current liabilities.", {}, "net_cash_definition"),
    ("Single-quarter operating cash flow is 43 billion.", {"cash": CASH}, "cash_period"),
    ("Other receivables are customer advances.", {}, "receivables_category"),
    ("FCF yield plus dividend yield gives total return of 8%.", {}, "return_double_count"),
])
def test_known_bad_claims_are_flagged_with_locations(report, statements, code):
    flags = financial_claim_warnings(report, statements)
    assert code in {w["code"] for w in flags}
    assert all(w["line"] == "1" and w["excerpt"] == report for w in flags)


@pytest.mark.parametrize("report", [
    "现金减流动负债不是净现金。",
    "其他应收款不是客户预收款，具体构成未提供。",
    "FCF收益率不能加上股息率当作股东总回报。",
    "本现金流列无法确认为2026Q2单季，不做加总。",
    "Net cash is not cash minus current liabilities.",
    "Other receivables are not customer advances.",
    "Ending 2026-06-30: Operating Cash Flow 43,780,858,849.93, source units; duration unknown.",
    "2026Q2单季收入375.8亿；现金流仅按截至日引用。",
])
def test_corrective_and_unrelated_statements_are_not_flagged(report):
    # The last example deliberately separates an income claim from cash timing.
    assert not financial_claim_warnings(report, {"cash": CASH})


def test_proven_three_month_cash_period_is_not_flagged():
    text = CASH.replace("2026-06-30", "2026-06-30 (3 months)").replace(
        "2026-03-31", "2026-03-31 (3 months)"
    )
    assert not financial_claim_warnings("2026Q2单季经营现金流437.8亿。", {"cash": text})


def test_review_annotation_keeps_raw_ai_reply_and_never_rescues_an_empty_stage():
    raw = "2026Q2单季经营现金流437.8亿。"
    state = {"company_of_interest": "600519.SS", "trade_date": "2026-10-09", "messages": [
        AIMessage(content="", tool_calls=[{"name": "get_cashflow", "id": "cash", "args": {}}]),
        ToolMessage(content=CASH, tool_call_id="cash"),
    ]}
    model = OfflineChatModel(responses=[AIMessage(content=raw)])
    result = create_fundamentals_analyst(model)(state)
    assert result["messages"][0].content == raw
    assert result["fundamentals_report"].startswith(raw + "\n")
    assert FINANCIAL_REVIEW_MARKER in result["fundamentals_report"]
    assert model.calls == 1
    assert annotate_financial_report("", state) == ""


@pytest.mark.parametrize("module,factory", [
    ("tradingagents.agents.researchers.bull_researcher", "create_bull_researcher"),
    ("tradingagents.agents.researchers.bear_researcher", "create_bear_researcher"),
    ("tradingagents.agents.managers.research_manager", "create_research_manager"),
    ("tradingagents.agents.trader.trader", "create_trader"),
    ("tradingagents.agents.risk_mgmt.aggressive_debator", "create_aggressive_debator"),
    ("tradingagents.agents.risk_mgmt.conservative_debator", "create_conservative_debator"),
    ("tradingagents.agents.risk_mgmt.neutral_debator", "create_neutral_debator"),
    ("tradingagents.agents.managers.portfolio_manager", "create_portfolio_manager"),
])
def test_accounting_definitions_reach_actual_downstream_prompts(module, factory):
    seen = []

    class Model:
        def invoke(self, prompt, *args, **kwargs):
            seen.append(str(prompt))
            return AIMessage(content="**Rating**: Hold\nEvidence needs review.")

        def with_structured_output(self, *args, **kwargs):
            raise NotImplementedError

    state = {
        "company_of_interest": "SPCX", "trade_date": "2026-10-09", "asset_type": "stock",
        "instrument_context": "SPCX", "market_report": "M", "sentiment_report": "S", "news_report": "N",
        "fundamentals_report": "F\n" + FINANCIAL_REVIEW_MARKER,
        "investment_plan": "P", "trader_investment_plan": "T", "past_context": "", "portfolio_context": "",
        "investment_debate_state": {"history": "", "count": 0, "bull_history": "", "bear_history": "", "current_response": ""},
        "risk_debate_state": {"history": "", "latest_speaker": "", "count": 0,
            "aggressive_history": "", "conservative_history": "", "neutral_history": "",
            "current_aggressive_response": "", "current_conservative_response": "", "current_neutral_response": ""},
    }
    getattr(importlib.import_module(module), factory)(Model())(state)
    prompt = "\n".join(seen)
    assert "cash minus current liabilities is not net cash" in prompt
    assert "FCF yield plus dividend yield is not shareholder total return" in prompt
    assert "do not reuse those claims" in prompt


def test_saved_report_screen_distinguishes_review_limited_and_not_checked(tmp_path):
    from tools_probe.check_run_complete import content_review

    path = tmp_path / "1_analysts" / "fundamentals.md"
    path.parent.mkdir()
    path.write_text("Legacy report", encoding="utf-8")
    assert content_review(tmp_path, ["fundamentals"]) == "NOT_CHECKED"
    path.write_text(annotate_financial_report("净现金=现金减流动负债。", {}), encoding="utf-8")
    assert content_review(tmp_path, ["fundamentals"]) == "REVIEW"
    path.write_text(annotate_financial_report("Only dated source facts.", {}), encoding="utf-8")
    assert content_review(tmp_path, ["fundamentals"]) == "LIMITED_CHECK_NO_FLAGS"
    assert content_review(tmp_path, ["market"]) == "NOT_CHECKED"


def test_annotation_cannot_turn_a_short_model_reply_into_a_complete_stage(tmp_path):
    from tools_probe.check_run_complete import judge

    path = tmp_path / "fundamentals.md"
    path.write_text(annotate_financial_report("净现金=现金减流动负债。", {}), encoding="utf-8")
    result = judge(path, "Analyst: fundamentals")
    assert not result["ok"]
    assert result["body_chars"] == len("净现金=现金减流动负债。")


def test_flagged_financial_premise_is_review_in_signal_and_memory_without_changing_model_decision(tmp_path):
    from tradingagents.agents.rating import run_rating
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.memory import TradingMemoryLog

    state = {"fundamentals_report": annotate_financial_report("净现金=现金减流动负债。", {}),
             "final_rating": "Buy", "final_trade_decision": "**Rating**: Buy\nAccumulate."}
    assert run_rating(state) == "REVIEW"
    graph = object.__new__(TradingAgentsGraph)
    graph._log_state = lambda *args: None
    graph.memory_log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    graph.record_decision("SPCX", "2026-10-09", state)
    assert graph.memory_log.load_entries()[0]["rating"] == "REVIEW"
    assert state["final_rating"] == "Buy" and state["final_trade_decision"].startswith("**Rating**: Buy")


def test_guard_returns_review_and_never_retries_a_content_warning(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    from tools_probe import guarded_run as guard
    from tradingagents.default_config import DEFAULT_CONFIG

    calls = []
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    monkeypatch.setattr(guard, "REPO", tmp_path)
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["guarded_run", "--no-probe", "--no-exchange-fix", "--max-retries", "3", "--"])
    monkeypatch.setattr(guard, "_run_cli", lambda args: (calls.append(args) or str(report_dir), False))
    monkeypatch.setitem(sys.modules, "structured_guard", SimpleNamespace(install=lambda: None))
    monkeypatch.setitem(sys.modules, "fin_context", SimpleNamespace(install=lambda: None))
    monkeypatch.setitem(sys.modules, "exchange_sessions", SimpleNamespace(DEGRADED=[]))
    monkeypatch.setitem(sys.modules, "prefetch_recorder", SimpleNamespace(install=lambda path: None))
    log = SimpleNamespace(path=tmp_path / "tool.log", start=0, end=0, close=lambda: None)
    monkeypatch.setitem(sys.modules, "run_toollog", SimpleNamespace(pin=lambda *args: log))
    monkeypatch.setitem(sys.modules, "probe_lib", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "check_run_complete", SimpleNamespace(check=lambda *args: {
        "ok": True, "content_review": "REVIEW", "results": [], "failed": [], "analyst_data": {},
    }))
    assert guard.main() == 2
    assert len(calls) == 1
    import json

    receipt = json.loads(next((tmp_path / "data/probe").rglob("attempt-1.json")).read_text(encoding="utf-8"))
    assert receipt["verdict"] == "ACCEPT"  # Stages and financial review are separate.
    assert receipt["content_review"] == "REVIEW"
