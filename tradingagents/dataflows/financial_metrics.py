"""Deterministic metrics from this run's statement messages, with source units."""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\s*\((\d+) months?\))?$")
_TITLE = re.compile(
    r"^# (Income Statement|Cash Flow(?: Statement)?|Balance Sheet)(?: data)? for (\S+) \(([^)]+)\)"
)
_UNIT = re.compile(r"\b([A-Z]{3}) in (millions|thousands|units)\b")
FINANCIAL_REVIEW_MARKER = "<!-- financial-semantics: REVIEW -->"
FINANCIAL_CHECK_MARKER = "<!-- financial-semantics: LIMITED_CHECK -->"
FINANCIAL_POLICY = (
    "Financial evidence rules: preserve each source's currency, scale, ending date and "
    "duration. A company profile's financialCurrency does not establish a statement's "
    "scale. A vendor's quarterly label alone does not establish a three-month cash flow. "
    "Do not turn undated-duration cash columns into Q1/Q2, H1, annual or TTM flows. "
    "Net cash requires an explicit cash and debt scope on the same balance-sheet date; "
    "cash minus current liabilities is not net cash. Other Receivables is an asset, "
    "not customer advances or contract liabilities; its business composition needs "
    "notes. FCF yield plus dividend yield is not shareholder total return and can "
    "double-count cash. Financing cash inflow alone does not prove equity issuance "
    "or dilution; operating/net income differences do not identify their cause. "
    "Missing tool data does not prove the company did not disclose it. Separate "
    "dated source facts, explicitly defined calculations and unverified hypotheses. "
    "A financial-semantics REVIEW annotation names claims requiring source verification: "
    "do not reuse those claims as established premises or cure them by repeating them. "
    "Absence of flags from this limited check is not factual approval."
)
_ALIASES = {
    "Revenue": ("Revenue", "Total Revenue"),
    "Gross Profit": ("Gross Profit",),
    "Operating Income": ("Operating Income", "Total Operating Income As Reported"),
    "Net Income": ("Net Income", "Net Income Common Stockholders"),
    "Operating Cash Flow": (
        "Operating Cash Flow",
        "Cash Flow From Continuing Operating Activities",
        "Total Cash From Operating Activities",
        "Cash Flowsfromusedin Operating Activities Direct",
    ),
    "Free Cash Flow": ("Free Cash Flow",),
    "Cash And Cash Equivalents": ("Cash And Cash Equivalents", "Cash and Equivalents"),
}


@dataclass
class Period:
    label: str
    end: date
    months: int | None
    values: dict[str, float | None] = field(default_factory=dict)


@dataclass
class Statement:
    frequency: str | None = None
    currency: str | None = None
    scale: str | None = None
    periods: list[Period] = field(default_factory=list)

    @property
    def unit(self) -> str:
        if self.currency and self.scale:
            return f"{self.currency} in {self.scale}"
        return "source units (currency/scale not stated)"


def parse(text: str | None) -> Statement:
    """Parse the multiline tool CSV; flattened display logs are not a data source."""
    statement = Statement()
    lines = (text or "").splitlines()
    for line in lines:
        title = _TITLE.match(line.strip())
        if title:
            statement.frequency = title[3].lower()
        unit = _UNIT.search(line)
        if unit and line.startswith("#"):
            statement.currency, statement.scale = unit.groups()
    body = "\n".join(line for line in lines if line.strip() and not line.startswith("#"))
    try:
        rows = list(csv.reader(io.StringIO(body)))
    except csv.Error:
        return statement
    if not rows or len(rows[0]) < 2:
        return statement
    columns: dict[int, Period] = {}
    for index, cell in enumerate(rows[0][1:], 1):
        match = _DATE.fullmatch(cell.strip())
        if not match:
            continue
        try:
            end = date.fromisoformat(match[1])
        except ValueError:
            continue
        months = int(match[2]) if match[2] else None
        period = Period(cell.strip(), end, months)
        columns[index] = period
        statement.periods.append(period)
    for row in rows[1:]:
        if not row:
            continue
        name = row[0].strip()
        for index, period in columns.items():
            value = None
            if index < len(row):
                try:
                    number = float(row[index])
                    value = number if math.isfinite(number) else None
                except ValueError:
                    pass
            period.values[name] = value
    return statement


def _value(period: Period, row: str) -> float | None:
    for name in _ALIASES[row]:
        value = period.values.get(name)
        if value is not None:
            return value
    return None


def _period_text(period: Period, statement: Statement) -> str:
    if period.months:
        return f"{period.months}-month cumulative as filed, ending {period.end}; start not supplied"
    return f"{statement.frequency or 'unknown frequency'} per source, ending {period.end}; start not supplied"


def _year_ago(period: Period, statement: Statement) -> Period | None:
    candidates = [
        candidate
        for candidate in statement.periods
        if candidate.end.year == period.end.year - 1
        and candidate.end.month == period.end.month
        and abs(candidate.end.day - period.end.day) <= 7
        and candidate.months == period.months
    ]
    return candidates[0] if len(candidates) == 1 else None


def _calendar_pair(statement: Statement, year: int, row: str) -> tuple[float, str] | None:
    # A vendor's quarterly schedule does not prove a three-month flow period.
    # Require an explicit duration before summing source cash-flow columns.
    if statement.frequency != "quarterly":
        return None
    pair = []
    for month, day in ((3, 31), (6, 30)):
        candidates = [
            p
            for p in statement.periods
            if p.end == date(year, month, day) and p.months == 3
        ]
        if len(candidates) != 1 or _value(candidates[0], row) is None:
            return None
        pair.append(candidates[0])
    total = sum(_value(p, row) for p in pair)
    return total, " + ".join(p.label for p in pair)


def _dated_facts(statement: Statement, label: str) -> list[str]:
    """Transpose recent source columns so each value carries its period and row."""
    if not statement.periods:
        return []
    periods = sorted(statement.periods, key=lambda p: (p.end, p.months or 0))[-8:]
    lines = [
        f"## {label}: source facts keyed by period (recent {len(periods)} columns)",
        f"Source frequency: {statement.frequency}; monetary units: {statement.unit}. "
        "Explicit row units take precedence; do not assign money units to EPS or share counts.",
        "Only these source columns exist in this extract. Missing quarters/fields are not zero "
        "and must not be filled in. Period starts are not supplied.",
    ]
    for period in periods:
        facts = "; ".join(
            f"{row}={value:,.2f}"
            for row, value in period.values.items()
            if value is not None
        )
        lines.append(f"- {period.label}: {facts or 'NOT AVAILABLE (all rows empty)'}")
    return lines


def _balance_metrics(statement: Statement) -> list[str]:
    """Keep debt, liabilities and asset categories distinct, even when values exist."""
    if not statement.periods:
        return []
    latest = max(statement.periods, key=lambda p: p.end)
    lines = [f"## Balance-sheet definitions, ending {latest.end}; {statement.unit}"]
    cash = _value(latest, "Cash And Cash Equivalents")
    debt = latest.values.get("Total Debt")
    if cash is not None and debt is not None:
        cash_row = next(
            row for row in _ALIASES["Cash And Cash Equivalents"]
            if latest.values.get(row) is not None
        )
        lines.append(
            f"- {cash_row} minus Total Debt: {cash - debt:,.2f} "
            f"({statement.unit}); derived on the same date using these exact source rows. "
            "Debt scope/restricted cash must be checked before calling this net cash."
        )
    else:
        lines.append(
            "- Cash minus debt: NOT AVAILABLE (cash/equivalents or Total Debt "
            "missing/empty on this date). Do not substitute Current Liabilities or Total Liabilities."
        )
    if latest.values.get("Other Receivables") is not None:
        lines.append(
            "- Other Receivables is an asset row; its composition is NOT AVAILABLE "
            "from this row alone. Do not rename it customer advances, contract "
            "liabilities or a dealer-demand reservoir without notes."
        )
    return lines


def build_summary(
    income_csv: str | None, cash_csv: str | None, balance_csv: str | None = None
) -> str:
    """Never guess currency, scale, period start, or a missing prior-year fact."""
    lines: list[str] = []
    income = parse(income_csv)
    cash = parse(cash_csv)
    if income.periods:
        last = max(income.periods, key=lambda p: (p.end, -(p.months or 3)))
        lines.append(f"## Income statement -- {_period_text(last, income)}; {income.unit}")
        revenue, gross, net = (
            _value(last, row) for row in ("Revenue", "Gross Profit", "Net Income")
        )
        for label, value in (
            ("Revenue", revenue),
            ("Gross profit", gross),
            ("Operating income", _value(last, "Operating Income")),
            ("Net income", net),
        ):
            lines.append(
                f"- {label}: {value:,.2f} ({income.unit})"
                if value is not None
                else f"- {label}: NOT AVAILABLE (row missing/empty)"
            )
        if revenue is not None and revenue != 0 and gross is not None:
            lines.append(f"- Gross margin (Gross Profit / Revenue): {100 * gross / revenue:.2f}%")
        # This row declares its own per-share unit; the statement's million-dollar
        # unit does not apply to it. Do not infer an EPS unit for other vendors.
        eps = last.values.get("Diluted EPS (USD/shares)")
        if eps is not None:
            lines.append(f"- Diluted EPS, {last.label}: {eps:.2f} USD/share (explicit row unit)")
        previous = _year_ago(last, income)
        prior = _value(previous, "Net Income") if previous else None
        if net is not None and prior not in (None, 0):
            lines.append(
                f"- Net income YoY against {previous.label}: {100 * (net - prior) / abs(prior):+.2f}%; matched by date and duration, never column position"
            )
        else:
            lines.append(
                "- Net income YoY: NOT AVAILABLE (no unambiguous matching prior-year value)"
            )
    elif income_csv:
        lines.append("## Income statement: NOT AVAILABLE (no parseable multiline statement)")
    if cash.periods:
        lines.append(f"## Cash flow -- {cash.unit}; source-labelled periods only")
        lines.append(
            "- A vendor label 'quarterly' is not proof of three-month cash flows. "
            "Without explicit durations/starts, quote ending dates only; do not "
            "label columns as calendar quarters or sum them into half-year cash flows."
        )
        last = max(cash.periods, key=lambda p: (p.end, -(p.months or 3)))
        lines.append(f"- Latest column: {_period_text(last, cash)}")
        for row in ("Operating Cash Flow", "Free Cash Flow"):
            value = _value(last, row)
            lines.append(
                f"- {row}, {last.label}: {value:,.2f} ({cash.unit})"
                if value is not None
                else f"- {row}: NOT AVAILABLE (row missing/empty)"
            )
            for year in sorted({p.end.year for p in cash.periods})[-2:]:
                cumulative = [p for p in cash.periods if p.end.year == year and p.months == 6]
                for period in cumulative:
                    if period is last:
                        continue
                    value = _value(period, row)
                    if value is not None:
                        lines.append(
                            f"- {row}, {_period_text(period, cash)}: {value:,.2f} ({cash.unit}); used as filed, not re-summed"
                        )
                pair = _calendar_pair(cash, year, row)
                if pair:
                    lines.append(
                        f"- {row}, sum of source quarterly columns {pair[1]}: {pair[0]:,.2f} ({cash.unit}); period starts not supplied"
                    )
    elif cash_csv:
        lines.append("## Cash flow: NOT AVAILABLE (no parseable multiline statement)")
    balance = parse(balance_csv)
    lines.extend(_balance_metrics(balance))
    for label, statement in (
        ("Income statement", income),
        ("Cash flow", cash),
        ("Balance sheet", balance),
    ):
        lines.extend(_dated_facts(statement, label))
    return "\n".join(lines)


def statements_from_state(state: dict[str, Any]) -> dict[str, str]:
    """Use matched tool calls/results in state only; never reopen a shared log."""
    ticker = str(state.get("company_of_interest", "")).upper()
    as_of = state.get("trade_date")
    if not ticker or not as_of:
        return {}
    calls: dict[str, dict] = {}
    statements: dict[str, str] = {}
    allowed = {
        "get_income_statement": "income",
        "get_cashflow": "cash",
        "get_balance_sheet": "balance",
    }
    for message in state.get("messages", []):
        if isinstance(message, AIMessage):
            calls.update({call["id"]: call for call in message.tool_calls})
        if not isinstance(message, ToolMessage):
            continue
        call = calls.get(message.tool_call_id)
        if not call or call.get("name") not in allowed:
            continue
        args = call.get("args", {})
        # InjectedState parameters are hidden from the model and absent from
        # AIMessage.tool_calls. Explicit tickers must match; when it is injected,
        # the matched result's title below still proves the active instrument.
        if "ticker" in args and str(args["ticker"]).upper() != ticker:
            continue
        if args.get("curr_date") not in (None, as_of):
            continue
        kind = allowed[call["name"]]
        statements.pop(kind, None)
        if message.status == "error":
            continue
        text = message.content
        if not isinstance(text, str):
            continue
        title = _TITLE.match(text.splitlines()[0]) if text.splitlines() else None
        if not title or title[2].upper() != ticker:
            continue
        title_kind = {
            "Income Statement": "income", "Cash Flow": "cash",
            "Cash Flow Statement": "cash", "Balance Sheet": "balance",
        }.get(title[1])
        if title_kind != kind:
            continue
        filed = re.search(r"filed on or before (\d{4}-\d{2}-\d{2})", text)
        if filed and filed[1] != as_of:
            continue
        statement = parse(text)
        if any(p.end.isoformat() > as_of for p in statement.periods):
            continue
        # Last result of the same tool in the current state wins, even if empty.
        statements[kind] = text
    return statements


def summary_from_state(state: dict[str, Any]) -> str:
    statements = statements_from_state(state)
    return build_summary(
        statements.get("income"), statements.get("cash"), statements.get("balance")
    )


def financial_claim_warnings(report: str, statements: dict[str, str]) -> list[dict[str, str]]:
    """Flag a few risky *assertions* for review, not certify a report's truth.

    This deliberately bounded Chinese/English screen reads matched statements,
    not profile/notes/news. A flag asks for verification, including other tools.
    It cannot exhaustively understand prose, tables, negation or causal claims.
    """
    warnings: list[dict[str, str]] = []
    cash = parse(statements.get("cash"))
    income = parse(statements.get("income"))
    balance = parse(statements.get("balance"))
    unknown_cash_duration = any(p.months is None for p in cash.periods)
    missing_debt = bool(balance.periods) and max(
        balance.periods, key=lambda p: p.end
    ).values.get("Total Debt") is None
    unknown_units = any(s.periods and (not s.currency or not s.scale) for s in (income, cash, balance))
    patterns = (
        ("cash_period", unknown_cash_duration,
         r"(?:单季|Q[1-4]|[一二三四]季度|single.quarter)[^。；;\n]{0,50}(?:现金流|资本开支|cash flow|\bOCF\b|\bFCF\b)|(?:现金流|资本开支|cash flow|\bOCF\b|\bFCF\b)[^。；;\n]{0,50}(?:单季|Q[1-4]|[一二三四]季度|single.quarter)",
         "现金流来源有期间长度不明的列；该季度/单季标签须核对具体列的起始日和长度。"),
        ("net_cash_definition", True,
         r"(?=.*(?:净现金|net cash))(?=.*(?:流动负债|current liabilities))",
         "现金减流动负债不能命名为净现金；需核对同日现金和债务的定义。"),
        ("net_cash_scope", missing_debt,
         r"(?:净现金|net cash).*\d",
         "匹配的资产负债表缺同日Total Debt；量化净现金须另给现金/债务范围和来源，不能补零。"),
        ("statement_units", unknown_units,
         r"以人民币元计|人民币元为单位|source.{0,30}(?:in CNY units|in RMB units)",
         "匹配的报表未声明币种/数量级；公司profile币种不能证明报表以人民币元计。"),
        ("receivables_category", True,
         r"(?=.*(?:其他应收|other receivables))(?=.*(?:预收|打款|蓄水池|customer advances|contract liabilities))",
         "其他应收款是资产；客户预付款/渠道蓄水池的解释须有附注支持。"),
        ("return_double_count", True,
         r"(?=.*(?:FCF|自由现金流))(?=.*(?:股息率|dividend yield))(?=.*(?:总回报|total return))(?=.*(?:加上|\+|plus|add))",
         "FCF收益率与股息率不能直接相加当作股东总回报，可能重复计算现金。"),
    )
    for number, raw in enumerate(report.splitlines(), 1):
        line = re.sub(r"[*_`]+", "", raw).strip()
        if not line:
            continue
        # Explicit corrective statements must not be mistaken for the bad claim.
        # Mixed assertions/negations remain a limitation, documented above.
        if re.search(r"不能|不可|不应|并非|不是|不得|无法|未提供|不做|not\b|cannot\b|do not\b", line, re.I):
            continue
        for code, applicable, pattern, reason in patterns:
            if applicable and re.search(pattern, line, re.I):
                warnings.append({"code": code, "line": str(number), "excerpt": raw[:400], "reason": reason})
    return warnings


def annotate_financial_report(report: str, state: dict[str, Any]) -> str:
    """Keep model text intact, append bounded review findings for downstream readers."""
    if not report.strip():
        return report  # A note must never turn a missing analyst reply into content.
    statements = statements_from_state(state)
    warnings = financial_claim_warnings(report, statements)
    marker = FINANCIAL_REVIEW_MARKER if warnings else FINANCIAL_CHECK_MARKER
    lines = [report, "", marker, "", "### 程序财务复核提示"]
    if warnings:
        lines.append("状态：REVIEW。以下原文表述需核对来源，后续分析不得直接当作已证实事实：")
        for warning in warnings:
            lines.append(f"- [{warning['code']}] 原文第{warning['line']}行：{warning['reason']}")
    else:
        lines.append("有限规则筛查未命中；不代表财务事实、单位、期间或因果解释已通过验收。")
    lines.append("范围：匹配本次标的和分析日的财务工具报表；不覆盖全部工具、附注或自然语言推断。")
    return "\n".join(lines)
