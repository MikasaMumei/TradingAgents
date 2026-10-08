r"""The pre-fetched sources must be visible, using the real node.

The bug this covers: the Sentiment Analyst fetches news, StockTwits and
Reddit inside its own node, before the model runs. None of it produces a tool
call, so a ledger built on tool calls calls the analyst unevidenced and a
sound run fails acceptance.

The node is driven with the three fetchers stubbed, so the real code path runs
and nothing touches the network.

    .venv\Scripts\python.exe tools_probe\test_prefetch_recording.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FAILURES = []


def check(name, ok, detail=""):
    print(
        "  [{}] {}{}".format(
            "PASS" if ok else "FAIL", name, ("  <- " + str(detail)) if not ok else ""
        )
    )
    if not ok:
        FAILURES.append(name)


def run_node(results, prefetch_log=None):
    """Drive the real sentiment node with stubbed fetchers."""
    from tradingagents.agents.analysts import sentiment_analyst as S

    calls = []

    def news_func(ticker, start_date, end_date):
        calls.append("get_news")
        return results["get_news"]

    def news_tool(ticker, start_date, end_date):
        raise AssertionError("the node should reach news through .func")

    # The node calls get_news.func(...), so the attribute is the real seam.
    news_tool.func = news_func

    def st(*a, **kw):
        calls.append("get_stocktwits")
        return results["get_stocktwits"]

    def rd(*a, **kw):
        calls.append("get_reddit")
        return results["get_reddit"]

    original = (
        S.get_news,
        S.fetch_stocktwits_messages,
        S.fetch_reddit_posts,
        S.subreddits_for,
        S.jev_screen,
    )
    S.get_news, S.fetch_stocktwits_messages, S.fetch_reddit_posts = news_tool, st, rd
    S.subreddits_for = lambda t: []
    S.jev_screen = lambda t: []

    class FakeLLM:
        def invoke(self, *a, **kw):
            m = type("M", (), {"content": "ok"})()
            return m

        def with_structured_output(self, *a, **kw):
            return None

    state = {
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
    # The recorder must be installed AFTER the stubs, because it wraps
    # whatever the module attribute holds at install time. Wrapping first and
    # stubbing after would silently discard the recorder.
    if prefetch_log is not None:
        import prefetch_recorder as PR

        # Each run installs fresh stubs, so the previous run's wrappers have to
        # come off first; otherwise the guard skips re-wrapping and the new
        # stubs are never seen by the recorder.
        for name in ("get_news", "fetch_stocktwits_messages", "fetch_reddit_posts"):
            fn = getattr(S, name, None)
            if callable(fn):
                fn.__dict__.pop("_prefetch_wrapped", None)
        PR.install(prefetch_log)

    node = S.create_sentiment_analyst(FakeLLM())
    try:
        # create_sentiment_analyst returns the node function itself
        node(state)
    except Exception as exc:
        print(f"     (node raised {type(exc).__name__}: {str(exc)[:60]})")
    finally:
        (
            S.get_news,
            S.fetch_stocktwits_messages,
            S.fetch_reddit_posts,
            S.subreddits_for,
            S.jev_screen,
        ) = original
    return calls


def main():
    import check_run_complete as C

    print("== the node really fetches all three ==")
    good = {
        "get_news": "## News\nAAPL reported record revenue on Tuesday, beating estimates.",
        "get_stocktwits": "$AAPL bulls loading on strong guidance and margin expansion",
        "get_reddit": "AAPL discussion thread with several posts from retail investors this week",
    }
    seen = run_node(good)
    check(
        "the node calls all three fetchers",
        set(seen) == {"get_news", "get_stocktwits", "get_reddit"},
        seen,
    )

    print()
    print("== and the recorder makes them visible ==")
    tmp = pathlib.Path(tempfile.mkdtemp())
    log = tmp / "prefetch.log"
    run_node(good, prefetch_log=log)
    check(
        "the recorder produced a log",
        log.exists() and log.read_text(encoding="utf-8").strip() != "",
    )
    text = log.read_text(encoding="utf-8")
    for method in ("get_news", "get_stocktwits", "get_reddit"):
        check(f"{method} is recorded", method in text, text[:200])
    check("records use the [Data] shape the ledger reads", "[Data]" in text)

    led = C.data_ledger(log)
    st = led["rollup"]["sentiment"]
    check("sentiment is no longer 'no-evidence'", st["state"] in ("ok", "partial"), st)
    check("all three count as delivered", st["ok"] == 3, st)
    check("and none of them was called a placeholder", st["refused"] == 0, st)

    print()
    print("== empty sources are recorded as refused, not delivered ==")
    log2 = tmp / "prefetch2.log"
    # The shapes a vendor actually returns when it has nothing: an empty string,
    # an angle-bracket placeholder, or a DATA_UNAVAILABLE: prefix. Invented prose
    # is not a refusal and must not be treated as one.
    run_node(
        {
            "get_news": "",
            "get_stocktwits": "<no StockTwits messages for $AAPL within 7d>",
            "get_reddit": "DATA_UNAVAILABLE: no vendor could serve get_reddit",
        },
        prefetch_log=log2,
    )
    led2 = C.data_ledger(log2)
    st2 = led2["rollup"]["sentiment"]
    check("nothing counts as delivered", st2["ok"] == 0, st2)
    check("real vendor placeholders are refused, not accepted", st2["refused"] >= 2, st2)
    check("and it is not left as usable evidence", st2["state"] in ("none", "unproven"), st2)
    check("a real vendor placeholder counts as refused, not ok", st2["refused"] >= 2, st2)

    print()
    print("== a second run is not polluted by the first ==")
    log3 = tmp / "prefetch3.log"
    run_node(good, prefetch_log=log3)
    run_node({"get_news": "", "get_stocktwits": "", "get_reddit": ""}, prefetch_log=log3)
    check(
        "each attempt wrote its own log, neither truncated",
        log3.exists() and log3.read_text(encoding="utf-8").count("get_news") >= 1,
        log3.read_text(encoding="utf-8")[:160] if log3.exists() else "no log",
    )

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES):d}):")
        for n in FAILURES:
            print("  -", n)
        return 1
    print("all pre-fetch recording checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
