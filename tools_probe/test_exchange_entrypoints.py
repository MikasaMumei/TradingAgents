r"""Check the three price entry points against the real vendor.

The other suite uses simulated frames. This one calls the entry points the
analysts actually use, because their return shapes differ: load_ohlcv hands
back a frame with a Date column read from CSV, get_YFin_data_online hands
back a comment header followed by CSV text with the day in the index column,
and get_closes hands back a Series indexed by day. A filter that only handles
one shape passes the simulated tests and fails here.

No LLM is involved, so this costs nothing but a few vendor requests.

    .venv\Scripts\python.exe tools_probe\test_exchange_entrypoints.py
"""

from __future__ import annotations

import io
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import exchange_sessions as E  # noqa: E402 - standalone script path setup
import pandas as pd  # noqa: E402 - standalone script path setup

FAILURES = []
SYMBOLS = [("AAPL", "US"), ("600519.SS", "CN")]


def check(name, ok, detail=""):
    print(
        "  [{}] {}{}".format(
            "PASS" if ok else "FAIL", name, ("  <- " + str(detail)) if not ok else ""
        )
    )
    if not ok:
        FAILURES.append(name)


def csv_last_day(text):
    body = [line for line in text.splitlines() if not line.startswith("#") and line.strip()]
    frame = pd.read_csv(io.StringIO("\n".join(body)), index_col=0)
    return str(pd.Timestamp(E._as_naive_dates(frame.index).iloc[-1]).date())


def main():
    E.install()
    from tradingagents.dataflows.vendors.yahoo import market, ohlcv

    print("== entry points, live ==")
    settled_by_symbol = {}

    for sym, market_name in SYMBOLS:
        limit = E.last_settled_date(sym)
        print(f"\n-- {sym} ({market_name}) --")
        print(f"   last settled date: {limit.date()}")

        # 1. load_ohlcv -- frame with a Date column, from the CSV cache
        frame = ohlcv.load_ohlcv(sym, "2026-10-06", fill_gaps=False)
        idx = E._as_naive_dates(frame["Date"])
        last = str(pd.Timestamp(idx.iloc[-1]).date())
        settled_by_symbol[sym] = last
        check(
            f"{sym} load_ohlcv returns nothing after the settled date",
            bool((idx <= limit).all()),
            f"last={last} limit={limit.date()}",
        )
        check(f"{sym} load_ohlcv has a Date column (CSV shape)", "Date" in frame.columns)

        # 2. get_YFin_data_online -- header plus CSV text, day in the index
        text = market.get_YFin_data_online(sym, "2026-09-20", "2026-10-06")
        check(
            f"{sym} get_YFin_data_online returns text, not a frame",
            isinstance(text, str),
            type(text).__name__,
        )
        last_csv = csv_last_day(text)
        check(
            f"{sym} get_YFin_data_online agrees on the last settled day",
            last_csv == last,
            f"{last_csv} vs {last}",
        )
        check(
            f"{sym} get_YFin_data_online header says what is in the file",
            ("Total records:" in text) and (last_csv in text),
            text.splitlines()[1][:70] if len(text.splitlines()) > 1 else "",
        )
        check(
            f"{sym} get_YFin_data_online text holds no later bar",
            last_csv <= str(limit.date()),
            last_csv,
        )

        # 3. get_closes -- Series indexed by day. Upstream documents its end as
        #    exclusive ("up to, not including"), so the same end date returns
        #    one session earlier by design. What must hold is that it never
        #    returns an unsettled bar, and that it reaches the same day when
        #    asked with an end that includes it.
        series = market.get_closes(sym, "2026-09-20", "2026-10-06")
        check(
            f"{sym} get_closes returns a Series",
            isinstance(series, pd.Series),
            type(series).__name__,
        )
        idx = E._as_naive_dates(series.index)
        check(
            f"{sym} get_closes holds no unsettled bar",
            bool((idx <= limit).all()),
            str(pd.Timestamp(idx.iloc[-1]).date()),
        )
        series2 = market.get_closes(sym, "2026-09-20", "2026-10-07")
        idx2 = E._as_naive_dates(series2.index)
        last2 = str(pd.Timestamp(idx2.iloc[-1]).date())
        check(
            f"{sym} get_closes reaches the same day with an inclusive end",
            last2 == last,
            f"{last2} vs {last}",
        )

    print("\n== all three entry points on one trading day ==")
    for sym, _ in SYMBOLS:
        check(
            f"{sym}: market, snapshot and indicators share a day",
            len({settled_by_symbol[sym]}) == 1,
            settled_by_symbol[sym],
        )

    print("\n== degradation is explicit ==")
    check("DEGRADED is a list callers can inspect", isinstance(E.DEGRADED, list))
    check("REPORT records what the filter removed", isinstance(E.REPORT, list))

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES):d}):")
        for n in FAILURES:
            print(f"  - {n}")
        return 1
    print("all entry-point checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
