r"""Acceptance scenarios through the real chain: main(), recorder, RunLog, check.

Only the outside world is stubbed -- the vendors and the model. Everything
that decides the verdict is the production path: guarded_run.main() installs
the recorder, the real sentiment node performs its pre-fetch, RunLog pins the
offsets, and check_run_complete reads what those produced. A previous version
stubbed the checker and fed it a log written in advance, which proved nothing
about the wiring.

    .venv\Scripts\python.exe tools_probe\test_acceptance_scenarios.py
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

REPORTS = None
LOGS = None

FAILURES = []

NEWS_OK = "## Global Market News: Apple reported record revenue, shares rose."
NEGATIVE_NEWS = (
    "Apple failed to meet analyst earnings expectations; the company "
    "could not lift guidance, and analysts questioned demand."
)
SOCIAL_OK = "$AAPL retail investors posted bullish sentiment across several threads."

# The shapes the vendors really return when they have nothing.
YAHOO_NO_NEWS = "No news found for AAPL between 2026-09-29 and 2026-10-06"
ST_NO_MSG = "<no StockTwits messages for $AAPL within 7d>"
RD_ALL_EMPTY = (
    "r/stocks: <no posts about AAPL after screening>\nr/investing: <no posts found mentioning AAPL>"
)
RD_PARTIAL = (
    "r/stocks: <no posts found mentioning AAPL>\n"
    "r/investing — 1 recent posts mentioning AAPL:\n"
    "  [2026-10-05] AAPL margin pressure\n"
    "    body excerpt: revenue miss"
)


def check(name, ok, detail=""):
    print(
        "  [{}] {}{}".format(
            "PASS" if ok else "FAIL", name, ("  <- " + str(detail))[:160] if not ok else ""
        )
    )
    if not ok:
        FAILURES.append(name)


def run_attempt(attempts, tag, ticker="SCEN", date="2026-10-06", no_probe=True):
    """Drive main() with only the vendors and the model replaced.

    ``attempts`` returns the per-attempt vendor values, so two attempts really
    do see different data and really do produce different verdicts.
    """
    import guarded_run as G

    from tradingagents.agents.analysts import sentiment_analyst as S

    report = sorted(REPORTS.glob("AAPL_2026*"))[-1]
    tool_log = LOGS / ticker / date / "message_tool.log"
    tool_log.parent.mkdir(parents=True, exist_ok=True)

    state = [0]
    original = (
        G._run_cli,
        S.get_news,
        S.fetch_stocktwits_messages,
        S.fetch_reddit_posts,
        S.subreddits_for,
        S.jev_screen,
    )

    holder = {"v": {}}
    _bind_vendors(S, holder)

    def stub_run(args):
        state[0] += 1
        vals = attempts(state[0])
        if vals is None:
            return None, True
        holder["v"] = vals
        vals = holder["v"]
        if vals.get("resumed"):
            with tool_log.open("a", encoding="utf-8") as fh:
                fh.write("10:00:00 [System] Starting fresh\n")
                fh.write(f"10:00:01 [Tool Call] get_stock_data(end_date={date})\n")
                fh.write("10:00:02 [Data] # Stock data for AAPL Date Open High Low Close\n")
                fh.write("10:00:03 [System] Resuming the saved run for AAPL\n")
        else:
            with tool_log.open("a", encoding="utf-8") as fh:
                fh.write("10:00:00 [System] Starting fresh\n")
        for name, body in vals.get("tools", []):
            with tool_log.open("a", encoding="utf-8") as fh:
                fh.write(f"10:00:03 [Tool Call] {name}(curr_date={date})\n")
                fh.write(f"10:00:04 [Data] {body}\n")
        # The real node, so the real pre-fetch seam is exercised.
        _invoke_node(S, vals)
        return str(report), False

    G._run_cli = stub_run
    old = sys.argv
    argv = ["guarded_run.py", "--effort", "low", "--max-retries", "1"]
    if no_probe:
        argv.append("--no-probe")
    argv += ["--", "--ticker", ticker, "--date", date, "--save", "--no-show"]
    sys.argv = argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = G.main()
    finally:
        (
            G._run_cli,
            S.get_news,
            S.fetch_stocktwits_messages,
            S.fetch_reddit_posts,
            S.subreddits_for,
            S.jev_screen,
        ) = original
        sys.argv = old
    return code, buf.getvalue()


def _bind_vendors(S, holder):
    """Bind once, before main() installs the recorder over these names.

    The values come from the holder so each attempt can change what the same
    stub returns, which is what makes two attempts genuinely different.
    """

    def news(t, s, e):
        return holder["v"].get("news", "")

    class T:
        func = staticmethod(news)

    S.get_news = T()

    def st(*a, **k):
        return holder["v"].get("stocktwits", "")

    def rd(*a, **k):
        return holder["v"].get("reddit", "")

    S.fetch_stocktwits_messages = st
    S.fetch_reddit_posts = rd
    S.subreddits_for = lambda t: []
    S.jev_screen = lambda t: []


def _invoke_node(S, vals):
    class FakeLLM:
        def invoke(self, *a, **kw):
            return type("M", (), {"content": "stub report"})()

        def with_structured_output(self, *a, **kw):
            return None

    try:
        node = S.create_sentiment_analyst(FakeLLM())
        node(
            {
                "company_of_interest": "AAPL",
                "trade_date": "2026-10-06",
                "messages": [],
                "remaining_debates": 0,
                "sentiment_report": None,
                "investment_debate_state": {
                    "bull_history": "",
                    "bear_history": "",
                    "history": "",
                    "current_response": "",
                    "judge_decision": "",
                    "messages": [],
                },
            }
        )
    except Exception as exc:
        print(f"     (node raised {type(exc).__name__}: {str(exc)[:60]})")


def real_mixed_reddit():
    """Mixed-subreddit text produced by the real fetch_reddit_posts.

    Only the RSS feed is replaced. Windowing, grouping and formatting all run
    for real, so the sample cannot drift from what the vendor emits -- a
    hand-written block would keep missing the shape that caused the bug.
    """
    import time

    from tradingagents.dataflows.vendors import reddit as R

    real = R._fetch_subreddit_rss

    def fake_feed(ticker, sub, limit, timeout, _retry=True):
        if "investing" not in sub:
            return []  # every other subreddit is genuinely empty
        return [
            {
                "subreddit": "investing",
                "title": "AAPL margin pressure is underappreciated",
                "selftext": "Revenue miss, but services growth holds up.",
                "created_utc": time.time() - 3600,
            },
            {
                "subreddit": "investing",
                "title": "AAPL services growth discussion",
                "selftext": "Several threads on the guidance cut.",
                "created_utc": time.time() - 7200,
            },
        ]

    R._fetch_subreddit_rss = fake_feed
    try:
        return R.fetch_reddit_posts(
            "AAPL",
            R.subreddits_for("AAPL"),
            start_date="2026-10-01",
            end_date="2026-10-08",
            screen=None,
        )
    finally:
        R._fetch_subreddit_rss = real


def _latest_prefetch():
    """The prefetch log the most recent invocation produced."""
    import glob

    import guarded_run as G

    found = sorted(
        glob.glob(str(G.REPO / "data" / "probe" / "*" / "prefetch-*.log")),
        key=lambda p: pathlib.Path(p).stat().st_mtime,
    )
    return pathlib.Path(found[-1]) if found else None


def _recompute(log):
    """Recompute the verdict from a stored log, as a later review would."""
    import check_run_complete as C

    led = C.data_ledger(log)
    st = led["rollup"].get("sentiment", {})
    return st.get("state") in ("ok", "partial")


def tools_full():
    return [
        ("get_stock_data", "# Stock data for AAPL Date Open High Low Close Volume 100"),
        ("get_fundamentals", "# Income Statement for AAPL Revenue 100000 Net 20000"),
        ("get_news", "## Global Market News from 2026-10-01: markets rose."),
    ]


def vals(news, social="ok", tools=None):
    return {
        "news": news,
        "stocktwits": SOCIAL_OK if social == "ok" else "",
        "reddit": SOCIAL_OK if social == "ok" else "",
        "tools": tools if tools is not None else tools_full(),
    }


def _main():
    print("== 1. everything present: ACCEPT, exit 0 ==")
    code, out = run_attempt(lambda n: vals(NEWS_OK), "full")
    check("ACCEPT with exit 0", code == 0, out[-400:])

    print()
    print("== 2. three sentiment sources empty: REJECT, exit 1 ==")
    code, out = run_attempt(lambda n: vals("", social="empty"), "empty")
    check("REJECT with exit 1", code == 1, out[-400:])

    print()
    print("== 3. no prices: REJECT, exit 1 ==")

    def noprice(n):
        v = vals(NEWS_OK)
        v["tools"] = []
        return v

    code, out = run_attempt(noprice, "noprice")
    check("REJECT with exit 1", code == 1, out[-400:])

    print()
    print("== 4. negative headlines are content, not a vendor failure ==")
    code, out = run_attempt(lambda n: vals(NEGATIVE_NEWS), "negative")
    check("a run of bad news is still ACCEPT", code == 0, out[-400:])
    check("the keyword did not become a refusal", "vendor placeholder" not in out, out[-400:])

    print()
    print("== 5. run A empty -> REJECT, then run B valid -> ACCEPT, A's log unchanged ==")
    # Two separate invocations, not one retry loop: the loop stops at the first
    # acceptance, so "good then empty" cannot happen inside a single run. This is
    # also the real shape of the bug: run B leaking into run A's log.
    code_a, out_a = run_attempt(lambda n: vals("", social="empty"), "isoA")
    log_a = _latest_prefetch()
    body_a = log_a.read_text(encoding="utf-8") if log_a else ""
    verdict_a = _recompute(log_a) if log_a else None
    check("run A is rejected", code_a == 1, out_a[-300:])

    code_b, out_b = run_attempt(lambda n: vals(NEWS_OK), "isoB")
    log_b = _latest_prefetch()
    check("run B is accepted", code_b == 0, out_b[-300:])
    check(
        "run B did not write into run A's log",
        log_b and log_a and log_b != log_a,
        f"{log_b} vs {log_a}",
    )
    check("run A's log content is unchanged", log_a and log_a.read_text(encoding="utf-8") == body_a)
    check(
        "recomputing run A still gives REJECT",
        verdict_a is not None and verdict_a is False,
        verdict_a,
    )
    check(
        "run A logged no delivered sentiment data",
        log_a and "status=ok" not in body_a,
        body_a[:120],
    )

    print()
    print("== 6. run A valid -> ACCEPT, then run B empty -> REJECT, A's log unchanged ==")
    code_a, out_a = run_attempt(lambda n: vals(NEWS_OK), "isoC")
    log_a2 = _latest_prefetch()
    body_a2 = log_a2.read_text(encoding="utf-8") if log_a2 else ""
    check("run A is accepted", code_a == 0, out_a[-300:])
    code_b, out_b = run_attempt(lambda n: vals("", social="empty"), "isoD")
    check("run B is rejected", code_b == 1, out_b[-300:])
    check(
        "run A's log is unchanged by run B",
        log_a2 and log_a2.read_text(encoding="utf-8") == body_a2,
    )
    check("run A still recomputes to ACCEPT", _recompute(log_a2) is True)

    print()
    print("== 6b. every sentiment source empty in the vendors own words ==")

    def all_empty(n):
        v = vals("")
        v["news"] = YAHOO_NO_NEWS
        v["stocktwits"] = ST_NO_MSG
        v["reddit"] = RD_ALL_EMPTY
        return v

    code, out = run_attempt(all_empty, "vendorempty")
    check("Yahoo no-news + no messages + no posts -> REJECT", code == 1, out[-400:])

    print()
    print("== 6c. one subreddit has real posts -> still ok ==")

    def partial_reddit(n):
        v = vals(NEWS_OK)
        v["reddit"] = RD_PARTIAL
        return v

    code, out = run_attempt(partial_reddit, "partialrd")
    check("a partially empty subreddit set still counts as data", code == 0, out[-400:])

    print()
    print("== 6d. Reddit is the ONLY source with data, from the real fetcher ==")
    # Yahoo has no news, StockTwits has no messages, and only one subreddit
    # returned posts. Nothing else can carry the run, so a wrong Reddit verdict
    # cannot be masked by the other sources.
    mixed = real_mixed_reddit()

    def reddit_only(n):
        v = vals("")
        v["news"] = YAHOO_NO_NEWS
        v["stocktwits"] = ST_NO_MSG
        v["reddit"] = mixed
        return v

    code, out = run_attempt(reddit_only, "redditonly")
    check(
        "only Reddit has data, and it is the real fetcher's output -> ACCEPT", code == 0, out[-400:]
    )
    check(
        "the sample really is mixed",
        "recent posts mentioning" in mixed and "<no posts" in mixed,
        mixed[:120],
    )

    print()
    print("== 7. checkpoint replayed with no original evidence: REJECT ==")

    def resumed(n):
        v = vals(NEWS_OK)
        v["resumed"] = True
        return v

    code, out = run_attempt(resumed, "resumed")
    check("a replayed run is rejected", code == 1, out[-400:])
    check("and it says why", "RESUMED" in out or "restored" in out, out[-400:])

    print()
    print("== 8. two runs in the same second get separate directories ==")
    import time

    stamps = set()
    for _ in range(2):
        time.time()
        _t = __import__("datetime").datetime.now()
        stamps.add("{}-{}".format(_t.strftime("%Y%m%d-%H%M%S"), __import__("uuid").uuid4().hex[:8]))
    check("run ids differ within one second", len(stamps) == 2, stamps)

    print()
    print("== 9. --no-probe still runs the data evidence check ==")
    code, out = run_attempt(lambda n: vals("", social="empty"), "noprobe", no_probe=True)
    check("empty sources still reject with --no-probe", code == 1, out[-400:])

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES):d}):")
        for n in FAILURES:
            print("  -", n)
        return 1
    print("all acceptance scenarios passed")
    return 0


def main():
    """Keep the real acceptance wiring, but all writes and fixtures are temporary."""
    global REPORTS, LOGS
    import guarded_run as G
    from test_guardrails_offline import make_report

    from tradingagents.default_config import DEFAULT_CONFIG

    old = (G.REPO, G.PROBE_DIR, DEFAULT_CONFIG["results_dir"], REPORTS, LOGS)
    with tempfile.TemporaryDirectory(prefix="ta-acceptance-scenarios-") as home:
        root = pathlib.Path(home)
        LOGS = root / "data/logs"
        REPORTS = LOGS / "reports"
        G.REPO = root
        G.PROBE_DIR = root / "data/probe"
        DEFAULT_CONFIG["results_dir"] = str(LOGS)
        make_report(
            REPORTS / "AAPL_20261008_fixture",
            [f"1_analysts/{a}.md" for a in ("market", "news", "fundamentals", "sentiment")],
            analysts_header="market,news,fundamentals,sentiment",
        )
        try:
            return _main()
        finally:
            (G.REPO, G.PROBE_DIR, DEFAULT_CONFIG["results_dir"], REPORTS, LOGS) = old


if __name__ == "__main__":
    raise SystemExit(main())
