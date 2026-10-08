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
    r"^# (Income Statement|Cash Flow(?: Statement)?)(?: data)? for (\S+) \(([^)]+)\)"
)
_UNIT = re.compile(r"\b([A-Z]{3}) in (millions|thousands|units)\b")
_ALIASES = {
    "Revenue": ("Revenue", "Total Revenue"),
    "Gross Profit": ("Gross Profit",),
    "Net Income": ("Net Income", "Net Income Common Stockholders"),
    "Operating Cash Flow": (
        "Operating Cash Flow",
        "Cash Flow From Continuing Operating Activities",
        "Total Cash From Operating Activities",
        "Cash Flowsfromusedin Operating Activities Direct",
    ),
    "Free Cash Flow": ("Free Cash Flow",),
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
    # Only a source explicitly labelling single quarters may be summed. An
    # Apple March 6-month column is never a calendar half-year or a quarter.
    if statement.frequency != "quarterly":
        return None
    pair = []
    for month, day in ((3, 31), (6, 30)):
        candidates = [
            p
            for p in statement.periods
            if p.end == date(year, month, day) and p.months in (None, 3)
        ]
        if len(candidates) != 1 or _value(candidates[0], row) is None:
            return None
        pair.append(candidates[0])
    total = sum(_value(p, row) for p in pair)
    return total, " + ".join(p.label for p in pair)


def build_summary(income_csv: str | None, cash_csv: str | None) -> str:
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
        for label, value in (("Revenue", revenue), ("Net income", net)):
            lines.append(
                f"- {label}: {value:,.2f} ({income.unit})"
                if value is not None
                else f"- {label}: NOT AVAILABLE (row missing/empty)"
            )
        if revenue is not None and revenue != 0 and gross is not None:
            lines.append(f"- Gross margin (Gross Profit / Revenue): {100 * gross / revenue:.2f}%")
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
    return "\n".join(lines)


def summary_from_state(state: dict[str, Any]) -> str:
    """Use matched tool calls/results in state only; never reopen a shared log."""
    ticker = str(state.get("company_of_interest", "")).upper()
    as_of = state.get("trade_date")
    if not ticker or not as_of:
        return ""
    calls: dict[str, dict] = {}
    statements: dict[str, str] = {}
    allowed = {"get_income_statement": "income", "get_cashflow": "cash"}
    for message in state.get("messages", []):
        if isinstance(message, AIMessage):
            calls.update({call["id"]: call for call in message.tool_calls})
        if not isinstance(message, ToolMessage):
            continue
        call = calls.get(message.tool_call_id)
        if not call or call.get("name") not in allowed:
            continue
        args = call.get("args", {})
        if str(args.get("ticker", "")).upper() != ticker:
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
        if (call["name"] == "get_income_statement") != (title[1] == "Income Statement"):
            continue
        filed = re.search(r"filed on or before (\d{4}-\d{2}-\d{2})", text)
        if filed and filed[1] != as_of:
            continue
        statement = parse(text)
        if any(p.end.isoformat() > as_of for p in statement.periods):
            continue
        # Last result of the same tool in the current state wins, even if empty.
        statements[kind] = text
    return build_summary(statements.get("income"), statements.get("cash"))
