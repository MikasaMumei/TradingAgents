"""Financial anchors from trimmed real tool CSVs, with run/period/unit isolation."""

import json
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests.test_structured_reply import OfflineChatModel
from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
from tradingagents.dataflows.financial_metrics import build_summary, parse, summary_from_state

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/financial-statements-20261008.json").read_text(
        encoding="utf-8"
    )
)


def messages(ticker, day="2026-10-08", income=None, cash=None):
    result = []
    for tool, content in (("get_income_statement", income), ("get_cashflow", cash)):
        if content is None:
            continue
        result.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": tool, "id": tool, "args": {"ticker": ticker, "curr_date": day}}
                    ],
                ),
                ToolMessage(content=content, name=tool, tool_call_id=tool),
            ]
        )
    return result


def state(ticker="AAPL", day="2026-10-08", **kwargs):
    return {
        "company_of_interest": ticker,
        "trade_date": day,
        "messages": messages(ticker, day, **kwargs),
    }


def test_aapl_date_alignment_and_explicit_units():
    output = build_summary(FIXTURES["AAPL::fin_income"], FIXTURES["AAPL::fin_cashflow"])
    assert "109,417.00 (USD in millions)" in output
    assert "50.06%" in output and "+27.12%" in output
    assert "against 2025-06-28" in output
    assert "6-month cumulative as filed, ending 2026-03-28" in output
    assert "2026H1" not in output


def test_moutai_sums_quarters_with_unknown_currency_not_usd():
    output = build_summary(FIXTURES["600519.SS::fin_income"], FIXTURES["600519.SS::fin_cashflow"])
    assert "11,523,065,222.10" in output
    assert "69,858,607,366.78" in output
    assert "13,119,061,031.33" in output  # Operating-CF alias must be found.
    assert "source units (currency/scale not stated)" in output
    assert "USD" not in output and "CNY" not in output


def test_empty_state_does_not_read_old_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = tmp_path / "data/logs/AAPL/2026-10-08/message_tool.log"
    log.parent.mkdir(parents=True)
    original = b"[Data] old run revenue must never be consumed"
    log.write_bytes(original)
    assert summary_from_state(state()) == ""
    assert log.read_bytes() == original


def test_wrong_ticker_and_previous_asof_are_rejected():
    current = state(income=FIXTURES["AAPL::fin_income"])
    current["company_of_interest"] = "600519.SS"
    assert summary_from_state(current) == ""
    current = state(day="2026-10-07", income=FIXTURES["AAPL::fin_income"])
    assert summary_from_state(current) == ""


def test_tool_call_date_must_match_active_run():
    current = state(income=FIXTURES["AAPL::fin_income"])
    current["messages"][0].tool_calls[0]["args"]["curr_date"] = "2026-10-07"
    assert summary_from_state(current) == ""


def test_later_failed_tool_result_does_not_reuse_previous_value():
    current = state(income=FIXTURES["AAPL::fin_income"])
    current["messages"] += [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_income_statement",
                    "id": "failed",
                    "args": {"ticker": "AAPL", "curr_date": "2026-10-08"},
                }
            ],
        ),
        ToolMessage(content="Vendor unavailable", status="error", tool_call_id="failed"),
    ]
    assert summary_from_state(current) == ""


def test_duplicate_end_dates_preserve_distinct_durations():
    csv = "# Income Statement for AAPL (quarterly), USD in millions\n,2026-06-27,2026-06-27 (9 months)\nRevenue,100,300\n"
    statement = parse(csv)
    assert len(statement.periods) == 2
    assert [p.values["Revenue"] for p in statement.periods] == [100, 300]


def test_zero_revenue_and_missing_prior_period_do_not_crash_or_guess():
    output = build_summary(
        "# Income Statement for AAPL (quarterly)\n,2026-06-27\nRevenue,0\nGross Profit,0\nNet Income,10\n",
        None,
    )
    assert "Revenue: 0.00" in output
    assert "Gross margin" not in output
    assert "YoY: NOT AVAILABLE" in output


def test_missing_quarter_is_not_presented_as_total():
    text = "# Cash Flow data for 600519.SS (quarterly)\n,2025-06-30\nFree Cash Flow,100\n"
    assert "sum of" not in build_summary(None, text)


def test_flattened_log_is_not_guessed_back_into_csv():
    text = FIXTURES["AAPL::fin_income"].replace("\n", " ")
    assert "NOT AVAILABLE" in build_summary(text, None)


def test_latest_cumulative_cashflow_is_printed_once():
    text = "# Cash Flow Statement for AAPL (quarterly), USD in millions\n,2026-03-28 (6 months)\nOperating Cash Flow,12345\n"
    output = build_summary(None, text)
    assert output.count("12,345.00") == 1
    assert "6-month cumulative as filed" in output
    assert "H1" not in output


def test_csv_parser_failure_degrades_to_unavailable(monkeypatch):
    import csv

    previous = csv.field_size_limit()
    try:
        csv.field_size_limit(32)
        text = "# Income Statement for AAPL (quarterly)\n,2026-06-27\n" + "x" * 100 + ",1\n"
        assert "NOT AVAILABLE" in build_summary(text, None)
    finally:
        csv.field_size_limit(previous)


def test_real_fundamentals_node_receives_metrics_on_second_round():
    first_call = messages("AAPL", income=FIXTURES["AAPL::fin_income"])[0]
    model = OfflineChatModel(responses=[first_call, AIMessage(content="Evidence-based report")])
    node = create_fundamentals_analyst(model)
    initial = state()
    initial["messages"] = [HumanMessage(content="Analyze AAPL")]
    result = node(initial)
    assert result["fundamentals_report"] == ""
    assert "109,417.00" not in str(model.prompts[0])
    second = state(income=FIXTURES["AAPL::fin_income"])
    second["messages"] = [initial["messages"][0]] + second["messages"]
    result = node(second)
    assert result["fundamentals_report"] == "Evidence-based report"
    assert "109,417.00" in model.prompts[1][0].content
    assert "50.06%" in model.prompts[1][0].content
    assert "+27.12%" in model.prompts[1][0].content
    assert model.calls == 2
