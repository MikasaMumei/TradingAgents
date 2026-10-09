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


def test_moutai_keeps_undated_duration_cash_columns_and_unknown_units():
    output = build_summary(FIXTURES["600519.SS::fin_income"], FIXTURES["600519.SS::fin_cashflow"])
    assert "Free Cash Flow=3,614,973,924.53" in output
    assert "Free Cash Flow=43,553,507,681.54" in output
    assert "8,809,195,646.38" in output
    assert "sum of source quarterly columns" not in output
    assert "source units (currency/scale not stated)" in output
    assert "USD" not in output and "CNY" not in output


def test_cash_columns_are_summed_only_with_explicit_three_month_durations():
    csv = "# Cash Flow data for X (quarterly)\n,2025-03-31 (3 months),2025-06-30 (3 months)\nOperating Cash Flow,10,20\nFree Cash Flow,8,16\n"
    output = build_summary(None, csv)
    assert "sum of source quarterly columns 2025-03-31 (3 months) + 2025-06-30 (3 months): 30.00" in output
    assert "sum of source quarterly columns" not in build_summary(None, csv.replace(" (3 months)", ""))


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


def test_state_injected_ticker_is_not_required_in_model_arguments():
    current = state(income=FIXTURES["AAPL::fin_income"])
    current["messages"][0].tool_calls[0]["args"].pop("ticker")
    output = summary_from_state(current)
    assert "109,417.00" in output and "29,789.00" in output
    assert "50.06%" in output and "+27.12%" in output
    current["company_of_interest"] = "600519.SS"
    assert summary_from_state(current) == ""  # The result title still must match.


def test_source_aligned_values_keep_the_explicit_eps_row_unit():
    text = (
        "# Income Statement for AAPL (quarterly), USD in millions\n"
        ",2025-06-28,2026-06-27\nRevenue,85777,109417\n"
        "Gross Profit,39678,54770\nOperating Income,25352,35695\n"
        "Net Income,23434,29789\nDiluted EPS (USD/shares),1.57,2.02\n"
    )
    output = build_summary(text, None)
    assert "Gross profit: 54,770.00 (USD in millions)" in output
    assert "Operating income: 35,695.00 (USD in millions)" in output
    assert "Diluted EPS, 2026-06-27: 2.02 USD/share" in output
    assert "USD/share" not in build_summary(text.replace(" (USD/shares)", ""), None)


def test_production_subgraph_injects_ticker_and_rebuilds_metrics(monkeypatch):
    from tradingagents.agents import tools
    from tradingagents.graph.analyst_execution import build_analyst_execution_plan
    from tradingagents.graph.setup import _analyst_graph

    requests = []

    def vendor(tool, ticker, frequency, day):
        requests.append((tool, ticker, frequency, day))
        key = "AAPL::fin_income" if tool == "get_income_statement" else "AAPL::fin_cashflow"
        return FIXTURES[key]

    monkeypatch.setattr(tools, "route_to_vendor", vendor)
    first = AIMessage(
        content="",
        tool_calls=[
            {"name": tool, "id": tool, "args": {"freq": "quarterly"}}
            for tool in ("get_income_statement", "get_cashflow")
        ],
    )
    model = OfflineChatModel(responses=[first, AIMessage(content="Grounded report")])
    spec = build_analyst_execution_plan(["fundamentals"]).specs[0]
    graph = _analyst_graph(spec, create_fundamentals_analyst(model), max_tool_rounds=20)
    initial = state()
    initial["messages"] = [HumanMessage(content="Analyze AAPL")]
    result = graph.invoke(initial)
    assert result["fundamentals_report"].startswith("Grounded report\n")
    assert "financial-semantics: LIMITED_CHECK" in result["fundamentals_report"]
    assert set(requests) == {
        ("get_income_statement", "AAPL", "quarterly", "2026-10-08"),
        ("get_cashflow", "AAPL", "quarterly", "2026-10-08"),
    }
    prompt = model.prompts[1][0].content
    assert "109,417.00" in prompt and "29,789.00" in prompt
    assert "50.06%" in prompt and "+27.12%" in prompt
    assert model.calls == 2


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
    assert output.count("- Operating Cash Flow, 2026-03-28 (6 months): 12,345.00") == 1
    assert "Operating Cash Flow=12,345.00" in output
    assert "6-month cumulative as filed" in output
    assert "H1" not in output


def test_dated_facts_preserve_missing_quarters_and_row_alignment():
    text = "# Income Statement for AAPL (quarterly), USD in millions\n,2025-12-27,2026-06-27\nRevenue,143756,109417\nNet Income,42097,29789\n"
    output = build_summary(text, None)
    assert "2025-12-27: Revenue=143,756.00; Net Income=42,097.00" in output
    assert "2026-06-27: Revenue=109,417.00; Net Income=29,789.00" in output
    assert "2026-03-28" not in output and "2025-09-27" not in output


def test_balance_sheet_requires_matched_tool_type_and_title():
    text = "# Balance Sheet for AAPL (quarterly), USD in millions\n,2026-06-27\nTotal Assets,383266\nStockholders Equity,107520\n"
    current = state()
    current["messages"] = [
        AIMessage(content="", tool_calls=[
            {"name": "get_balance_sheet", "id": "balance", "args": {"freq": "quarterly"}}
        ]),
        ToolMessage(content=text, name="get_balance_sheet", tool_call_id="balance"),
    ]
    assert "Total Assets=383,266.00; Stockholders Equity=107,520.00" in summary_from_state(current)
    current["messages"][0].tool_calls[0]["name"] = "get_cashflow"
    assert summary_from_state(current) == ""


def test_source_facts_keep_only_recent_eight_columns():
    dates = [f"{year}-12-31" for year in range(2015, 2027)]
    text = "# Income Statement for AAPL (annual)\n," + ",".join(dates) + "\nRevenue," + ",".join(str(y) for y in range(12))
    output = build_summary(text, None)
    assert "2015-12-31:" not in output and "2018-12-31:" not in output
    assert "2019-12-31: Revenue=4.00" in output
    assert "2026-12-31: Revenue=11.00" in output


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
    assert result["fundamentals_report"].startswith("Evidence-based report\n")
    assert result["messages"][0].content == "Evidence-based report"
    assert "109,417.00" in model.prompts[1][0].content
    assert "50.06%" in model.prompts[1][0].content
    assert "+27.12%" in model.prompts[1][0].content
    assert model.calls == 2
