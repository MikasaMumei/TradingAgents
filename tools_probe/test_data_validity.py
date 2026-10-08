import pathlib
import sys
import tempfile

sys.path.insert(0, "tools_probe")
import check_run_complete as C
import run_toollog as R

FAIL = []


def check(n, ok, d=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", n, ("  <- " + str(d)) if not ok else ""))
    if not ok:
        FAIL.append(n)


def rep():
    root = pathlib.Path(tempfile.mkdtemp())
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
        "1_analysts/fundamentals.md",
    ]:
        t = root / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("# s\n" + ("body " * 60) + "\n", encoding="utf-8")
    (root / "complete_report.md").write_text(
        "# r\n- Rating: Hold\n- Analysts: market, fundamentals; rounds 2\n", encoding="utf-8"
    )
    return root


def log_with(lines):
    return type("L", (), {"read": staticmethod(lambda: lines)})()


print("== withheld is detected, not just DATA_UNAVAILABLE ==")
withheld = [
    "12:00:00 [Data] # Company Fundamentals for 600519.SS # Point-in-time as of: 2026-10-06  Profile fundamentals are withheld for this date.",
    "12:00:01 [Data] # Balance Sheet for 600519.SS # Point-in-time as of: 2026-10-06  Balance Sheet data is withheld for this date.",
    "12:00:02 [Data] # Cash Flow for 600519.SS # Point-in-time as of: 2026-10-06  Cash Flow data is withheld for this date.",
    "12:00:03 [Data] # Income Statement for 600519.SS # Point-in-time as of: 2026-10-06  Income Statement data is withheld for this date.",
    "12:00:04 [Data] # Insider Transactions for 600519.SS # Point-in-time as of: 2026-10-06  Insider transactions are withheld for this date.",
]
gaps = C.data_gaps(log_with(withheld))
keys = {g["key"] for g in gaps}
check(
    "all five categories are recognised",
    keys
    == {
        "Company Fundamentals",
        "Balance Sheet",
        "Cash Flow",
        "Income Statement",
        "Insider Transactions",
    },
    keys,
)
check(
    "they are filed as withheld, not unavailable",
    all(g["reason"] == "withheld" for g in gaps),
    [g["reason"] for g in gaps],
)

print()
print("== stages complete but data valid are separate claims ==")
v = C.check(rep(), None, log_with(withheld))
check("every stage still has text", v["stages_ok"] is True)
check(
    "fundamentals is reported as having no input",
    v["analyst_data"]["fundamentals"]["state"] == "none",
    v["analyst_data"]["fundamentals"],
)
check(
    "fundamentals is listed as starved",
    "fundamentals" in v["starved_analysts"],
    v["starved_analysts"],
)
check("the run is REJECTED even though the stages passed", v["ok"] is False)
check(
    "market is unaffected",
    v["analyst_data"]["market"]["state"] != "none",
    v["analyst_data"]["market"],
)

print()
print("== a run with data is unaffected by the rule ==")
good = [
    "12:00:00 [Data] # Company Fundamentals for AAPL # as of 2026-10-06  Revenue 100",
    "12:00:01 [Data] no configured vendor could serve get_news right now",
]
v2 = C.check(
    rep(),
    None,
    log_with([line if line.startswith("12") and "[Data]" in line else line for line in good]),
)
check(
    "a run that got its fundamentals data is not starved",
    "fundamentals" not in (v2["starved_analysts"] or []),
    v2["starved_analysts"],
)

print()
print("== per-attempt degradation does not leak ==")
import exchange_sessions as E  # noqa: E402 - standalone script path setup

E.DEGRADED.clear()
E.DEGRADED.append({"symbol": "AAPL", "stage": "refresh", "error": "first attempt only"})
first = list(E.DEGRADED)
E.DEGRADED.clear()  # 第二次 attempt 开头会清空
second = list(E.DEGRADED)
check("the first attempt record is still available to the caller", len(first) == 1)
check("the second attempt starts clean", second == [], second)

print()
print("== the log slice is bounded at both ends ==")
root = pathlib.Path(tempfile.mkdtemp())
args = ["--ticker", "AAPL", "--date", "2026-10-06"]
a1 = R.pin(args, root)
a1.path.parent.mkdir(parents=True, exist_ok=True)
with open(a1.path, "a", encoding="utf-8") as fh:
    fh.write("FIRST\n")
a1.close()
with open(a1.path, "a", encoding="utf-8") as fh:
    fh.write("LATER RUN\n")
check("the closed slice does not pick up later lines", a1.read() == ["FIRST"], a1.read())
a2 = R.pin(args, root)
with open(a2.path, "a", encoding="utf-8") as fh:
    fh.write("SECOND\n")
a2.close()
check("the next run sees only its own line", a2.read() == ["SECOND"], a2.read())
check("a closed slice is re-readable unchanged", a1.read() == ["FIRST"])

print()
if FAIL:
    print(f"FAILED ({len(FAIL):d}):")
    for n in FAIL:
        print("  -", n)
    raise SystemExit(1)
print("all data-validity and per-attempt checks passed")
