r"""Refresh failure must refuse, not serve the mid-session values.

A cache written before its own bar settled holds a price that is not a close.
Falling back to it would be worse than failing: the report would carry a number
that looks like an answer. These tests hold the line at that boundary, and check
that the reason survives into the verdict rather than dying with the exception.

    .venv\Scripts\python.exe tools_probe\test_refresh_failure.py
"""

import pathlib
import sys
import tempfile
from pathlib import Path

import check_run_complete as C
import exchange_sessions as E
import pandas as pd

from tradingagents.dataflows.errors import VendorUnavailableError

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _log(lines):
    return type("L", (), {"read": staticmethod(lambda: lines)})()


# A log where every key analyst actually got something.
EVIDENCE = [
    "10:00:00 [Tool Call] get_stock_data(end_date=2026-10-06)",
    "10:00:01 [Data] # Stock data for AAPL Open High Low Close Volume",
    "10:00:02 [Tool Call] get_news(end_date=2026-10-06)",
    "10:00:03 [Data] ## Global Market News, from 2026-10-01 to 2026-10-06: ### stocks rose",
    "10:00:04 [Data] ## FRED: Federal Funds Effective Rate (FEDFUNDS) - Units: %",
    "10:00:05 [Tool Call] get_fundamentals(curr_date=2026-10-06)",
    "10:00:06 [Data] # Balance Sheet for AAPL # as of 2026-10-06  Total Assets 100",
]

FAIL = []


def check(n, ok, d=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", n, ("  <- " + str(d)) if not ok else ""))
    if not ok:
        FAIL.append(n)


tmp = pathlib.Path(tempfile.mkdtemp())
cache = tmp / "AAPL-YFin-data.csv"
cache.write_text("Date,Close\n2026-10-06,333.35\n")

settled = pd.DataFrame(
    {"Date": pd.to_datetime(["2026-10-02", "2026-10-06"]), "Close": [330.0, 333.63]}
)

print("== refresh failure refuses to serve the old frame ==")
E.DEGRADED.clear()


def boom(*a, **k):
    raise RuntimeError("vendor unreachable")


try:
    E._refresh_cache_frame("AAPL", "2026-10-06", str(cache), boom)
    check("a failed refresh raises", False, "no exception")
except VendorUnavailableError as exc:
    check("a failed refresh raises VendorUnavailableError", True)
    check(
        "the message says it refused mid-session values",
        "Refusing to serve mid-session values" in str(exc),
        str(exc)[:90],
    )
check("the reason reaches DEGRADED", len(E.DEGRADED) == 1, E.DEGRADED)
check(
    "DEGRADED names symbol, stage and reason",
    E.DEGRADED[0]["symbol"] == "AAPL"
    and E.DEGRADED[0]["stage"] == "refresh"
    and "vendor unreachable" in E.DEGRADED[0]["error"],
)

print()
print("== empty refetch also refuses ==")
cache.write_text("Date,Close\n2026-10-06,333.35\n")
E.DEGRADED.clear()
try:
    E._refresh_cache_frame("AAPL", "2026-10-06", str(cache), lambda *a, **k: pd.DataFrame())
    check("an empty refetch raises", False, "no exception")
except VendorUnavailableError:
    check("an empty refetch raises", True)
check("the empty refetch is recorded", len(E.DEGRADED) == 1, E.DEGRADED)

print()
print("== fill_gaps is the caller's ==")
cache.write_text("Date,Close\n2026-10-06,333.35\n")
E.DEGRADED.clear()
E.REPORT.clear()
seen = {}


def spy(symbol, as_of_date, fill_gaps=True, **k):
    seen["fill_gaps"] = fill_gaps
    return settled


for want in (True, False):
    E._refresh_cache_frame("AAPL", "2026-10-06", str(cache), spy, want)
    check(f"fill_gaps={want} is forwarded", seen["fill_gaps"] is want, seen)
check(
    "REPORT records which fill_gaps the refresh used",
    E.REPORT and E.REPORT[-1].get("fill_gaps") is False,
    E.REPORT[-1:] if E.REPORT else "",
)

print()
print("== degradation travels into the verdict ==")
rep = pathlib.Path(tempfile.mkdtemp())
for rel in [
    "2_research/manager.md",
    "2_research/bull.md",
    "2_research/bear.md",
    "3_trading/trader.md",
    "4_risk/aggressive.md",
    "4_risk/conservative.md",
    "4_risk/neutral.md",
    "5_portfolio/decision.md",
    "1_analysts/market.md",
]:
    t = rep / rel
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text("# s\n" + ("body " * 60) + "\n", encoding="utf-8")
v = C.check(
    rep,
    ["market"],
    _log(EVIDENCE),
    [{"symbol": "AAPL", "stage": "refresh", "error": "vendor unreachable"}],
)
check(
    "a run with data still accepts alongside a degradation note",
    v["ok"] is True,
    v.get("starved_analysts"),
)
check("the degradation reaches the verdict", len(v.get("degraded") or []) == 1, v.get("degraded"))
check(
    "the note keeps symbol and reason",
    v["degraded"][0]["symbol"] == "AAPL" and "vendor unreachable" in v["degraded"][0]["error"],
)

print()
if FAIL:
    print(f"FAILED ({len(FAIL):d}):")
    for n in FAIL:
        print("  -", n)
    raise SystemExit(1)
print("all refresh-failure checks passed")
