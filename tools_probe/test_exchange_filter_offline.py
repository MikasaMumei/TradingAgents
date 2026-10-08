r"""Offline checks for the session filter. No network, no quota.

Time is injected, so each scenario is deterministic: a New York session in
progress, a cache written before the close, a frame holding only an unsettled
row, and a Shanghai holiday.

    .venv\Scripts\python.exe tools_probe\test_exchange_filter_offline.py
"""

from __future__ import annotations

import datetime as _dt
import os
import pathlib
import sys
import tempfile
import zoneinfo

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import exchange_sessions as E  # noqa: E402 - standalone script path setup
import pandas as pd  # noqa: E402 - standalone script path setup

FAILURES = []


def check(name, ok, detail=""):
    print(
        "  [{}] {}{}".format(
            "PASS" if ok else "FAIL", name, ("  <- " + str(detail)) if not ok else ""
        )
    )
    if not ok:
        FAILURES.append(name)


NY = zoneinfo.ZoneInfo("America/New_York")
SH = zoneinfo.ZoneInfo("Asia/Shanghai")


def frame(dates, closes):
    return pd.DataFrame({"Date": pd.to_datetime(dates), "Close": closes})


def main():
    print("== session filter ==")

    # --- last settled date depends on the exchange, not the machine
    during = _dt.datetime(2026, 10, 6, 11, 30, tzinfo=NY)  # 11:30 ET, open
    after = _dt.datetime(2026, 10, 6, 17, 30, tzinfo=NY)  # 17:30 ET, settled
    check(
        "US bar is unsettled during the session",
        str(E.last_settled_date("AAPL", during).date()) == "2026-10-05",
        E.last_settled_date("AAPL", during),
    )
    check(
        "US bar settles after the close plus the buffer",
        str(E.last_settled_date("AAPL", after).date()) == "2026-10-06",
        E.last_settled_date("AAPL", after),
    )

    sh_during = _dt.datetime(2026, 10, 6, 11, 0, tzinfo=SH)  # 11:00 CST, open
    sh_after = _dt.datetime(2026, 10, 6, 16, 0, tzinfo=SH)  # 16:00 CST, settled
    check(
        "A-share bar is unsettled before its close",
        str(E.last_settled_date("600519.SS", sh_during).date()) == "2026-10-05",
        E.last_settled_date("600519.SS", sh_during),
    )
    check(
        "A-share bar settles after its close",
        str(E.last_settled_date("600519.SS", sh_after).date()) == "2026-10-06",
        E.last_settled_date("600519.SS", sh_after),
    )

    # --- a US listing asked at a Shanghai afternoon is still judged by New York
    sh_afternoon = _dt.datetime(2026, 10, 6, 23, 0, tzinfo=SH)
    check(
        "machine timezone does not decide a US listing",
        str(E.last_settled_date("AAPL", sh_afternoon).date()) == "2026-10-05",
        E.last_settled_date("AAPL", sh_afternoon),
    )

    # --- an unidentified exchange is a question, not a pass
    try:
        E.last_settled_date("FOO.QQ")
        check("unknown exchange raises", False, "no exception")
    except E.ExchangeUnknown:
        check("unknown exchange raises", True)

    # --- dropping the in-progress bar, keeping range clipping
    _dt.datetime.now(_dt.UTC)
    f = frame(["2026-10-02", "2026-10-05", "2026-10-06"], [1.0, 2.0, 3.0])
    kept = E.filter_frame(f, "AAPL", now=during)
    check("the in-progress row is dropped", len(kept) == 2, len(kept))
    check(
        "the settled rows are kept in order", list(kept["Close"]) == [1.0, 2.0], list(kept["Close"])
    )

    # --- a frame of only unsettled rows must not come back as data
    only_new = frame(["2026-10-06"], [3.0])
    try:
        E.filter_frame(only_new, "AAPL", now=during)
        check("an all-unsettled frame raises instead of returning", False, "no exception")
    except E.NoSettledBar:
        check("an all-unsettled frame raises instead of returning", True)

    # --- a cache written before the close holds a mid-flight row
    tmp = pathlib.Path(tempfile.mkdtemp())
    cache = tmp / "AAPL-YFin-data.csv"
    frame(["2026-10-02", "2026-10-05"], [1.0, 2.0]).to_csv(cache, index=False)
    # pretend it was written an hour into the 6 Oct session
    stale = _dt.datetime(2026, 10, 6, 11, 0, tzinfo=NY).astimezone(_dt.UTC).timestamp()
    os.utime(cache, (stale, stale))
    E.SESSIONS[""]
    E.SESSIONS[""] = ("America/New_York", "16:00")
    # after the close, 2026-10-06 counts as settled -- but the cache predates it
    after_close = _dt.datetime(2026, 10, 6, 17, 0, tzinfo=NY)
    kept = E.filter_frame(
        frame(["2026-10-02", "2026-10-05", "2026-10-06"], [1.0, 2.0, 3.0]),
        "AAPL",
        cache_path=cache,
        what="test",
        now=after_close,
    )
    check(
        "a pre-close cache loses its newest row even after the close",
        len(kept) == 2,
        kept["Date"].tolist(),
    )

    # --- written after the close, the same row is kept
    fresh = _dt.datetime(2026, 10, 6, 17, 0, tzinfo=NY).astimezone(_dt.UTC).timestamp()
    os.utime(cache, (fresh, fresh))
    kept = E.filter_frame(
        frame(["2026-10-02", "2026-10-05", "2026-10-06"], [1.0, 2.0, 3.0]),
        "AAPL",
        cache_path=cache,
        what="test",
        now=after_close,
    )
    check("a post-close cache keeps its newest row", len(kept) == 3, len(kept))

    # --- the raw entry points are actually wrapped
    from tradingagents.dataflows.vendors.yahoo import market, ohlcv, snapshot

    E.install()
    check("load_ohlcv is wrapped", getattr(ohlcv, "_exchange_settle_patched", False))
    check(
        "get_YFin_data_online is wrapped",
        market.get_YFin_data_online.__name__ == "get_YFin_data_online"
        and "install.<locals>" in market.get_YFin_data_online.__qualname__,
    )
    check("get_closes is wrapped", "install.<locals>" in market.get_closes.__qualname__)
    check("snapshot sees the same loader", snapshot.load_ohlcv is ohlcv.load_ohlcv)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES):d}):")
        for n in FAILURES:
            print(f"  - {n}")
        return 1
    print("all exchange-filter checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
