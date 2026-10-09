r"""Keep unsettled sessions out of every price path, not just the cached one.

``load_ohlcv`` is the cached path. Two entry points bypass it and hit Yahoo
directly: ``get_YFin_data_online`` (the range query the market analyst asks
for) and ``get_closes`` (settlement). A filter on the cached path alone leaves
those answering with a live candle whose ``Close`` is the current price.

The rules, applied to each of the three:

* A session's daily bar counts only once the exchange has closed it, judged in
  the exchange's own timezone plus a settle buffer -- not on the machine clock.
  A box in Asia/Shanghai has to answer the New York question.
* A cache file written while the session was open holds that session's candle
  mid-flight. Its write time says so, and the row goes.
* If the filter leaves nothing, the answer is nothing. Returning the
  unfiltered rows is exactly the failure this exists to prevent.
* If the exchange cannot be identified, or the filter itself raises, the call
  fails loudly. "Could not check" is never "fine".

Runtime only: the checkout stays byte-identical to upstream.
"""

from __future__ import annotations

import datetime as _dt
import os
import zoneinfo

import numpy as np
import pandas as pd

# Suffix -> (IANA timezone, session close HH:MM local). A bare symbol is a US
# listing. Scope is deliberately the two markets under test plus the rest of
# the earlier table; unknown symbols are reported, never assumed open.
SESSIONS = {
    "": ("America/New_York", "16:00"),
    ".SS": ("Asia/Shanghai", "15:00"),
    ".SZ": ("Asia/Shanghai", "15:00"),
    ".BJ": ("Asia/Shanghai", "15:00"),
    ".HK": ("Asia/Hong_Kong", "16:00"),
    ".T": ("Asia/Tokyo", "15:00"),
    ".KS": ("Asia/Seoul", "15:30"),
    ".KQ": ("Asia/Seoul", "15:30"),
    ".TW": ("Asia/Taipei", "13:30"),
    ".TWO": ("Asia/Taipei", "13:30"),
    ".NS": ("Asia/Kolkata", "15:30"),
    ".BO": ("Asia/Kolkata", "15:30"),
    ".SI": ("Asia/Singapore", "17:00"),
    ".L": ("Europe/London", "16:30"),
    ".DE": ("Europe/Berlin", "17:30"),
    ".PA": ("Europe/Paris", "17:30"),
    ".AS": ("Europe/Amsterdam", "17:30"),
    ".SW": ("Europe/Zurich", "17:30"),
    ".MI": ("Europe/Rome", "17:30"),
    ".TO": ("America/Toronto", "16:00"),
    ".AX": ("Australia/Sydney", "16:00"),
    ".SA": ("America/Sao_Paulo", "17:00"),
}

# The vendor needs a moment to settle and publish. Over-generous on purpose:
# waiting costs one bar, being early costs a wrong close.
SETTLE_BUFFER = _dt.timedelta(minutes=45)

REPORT = []


class ExchangeUnknown(RuntimeError):
    """The symbol's exchange could not be identified."""


class NoSettledBar(RuntimeError):
    """Everything in the requested range was still unsettled."""


def session_for(symbol: str):
    if not symbol or not str(symbol).strip():
        return None
    text = str(symbol).strip()
    for suffix in sorted((s for s in SESSIONS if s), key=len, reverse=True):
        if text.upper().endswith(suffix.upper()):
            return SESSIONS[suffix]
    if "." not in text:
        return SESSIONS[""]
    return None


def _tz_and_close(symbol):
    found = session_for(symbol)
    if found is None:
        raise ExchangeUnknown(f"no exchange table entry for {symbol!r}")
    try:
        return zoneinfo.ZoneInfo(found[0]), found[1]
    except Exception as exc:
        raise ExchangeUnknown(f"timezone {found[0]!r} unusable for {symbol!r}: {exc}") from exc


def last_settled_date(symbol: str, now: _dt.datetime | None = None) -> pd.Timestamp:
    """The most recent date whose daily bar is certainly final.

    Raises ExchangeUnknown rather than guessing: an unidentified exchange is
    a question, and answering it silently is how a partial bar gets through.
    """
    tz, close_hm = _tz_and_close(symbol)
    hour, minute = (int(part) for part in close_hm.split(":"))
    local_now = (now or _dt.datetime.now(_dt.UTC)).astimezone(tz)
    close_at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_now < close_at + SETTLE_BUFFER:
        return pd.Timestamp((local_now.date() - _dt.timedelta(days=1)).isoformat())
    return pd.Timestamp(local_now.date().isoformat())


def _settle_instant(date, symbol) -> _dt.datetime:
    tz, close_hm = _tz_and_close(symbol)
    hour, minute = (int(part) for part in close_hm.split(":"))
    local = _dt.datetime.combine(
        _dt.date.fromisoformat(str(date)[:10]), _dt.time(hour, minute), tzinfo=tz
    )
    return (local + SETTLE_BUFFER).astimezone(_dt.UTC)


def _as_naive_dates(values) -> pd.Series:
    # Accepts a Series column or a DatetimeIndex: the CSV cache arrives as a
    # column, the direct vendor paths arrive indexed by day. .dt exists only on a
    # Series, so the Index case has to be routed through one.
    series = values if isinstance(values, pd.Series) else pd.Series(values)
    # Keep the local calendar date. Converting to UTC first shifts the day backwards
    # for any positive offset -- a Shanghai bar stamped 00:00+08:00 lands on the
    # previous day in UTC -- so the tz is dropped, not converted.
    return pd.to_datetime(series, errors="coerce").dt.tz_localize(None)


def filter_frame(frame, symbol: str, *, cache_path=None, what="frame", now=None):
    """Drop everything after the last settled session. Never widens the range.

    ``cache_path`` additionally drops the newest row when the cache holding it
    was written before that session could settle.
    """
    settled = last_settled_date(symbol, now)
    if frame is None or len(frame) == 0:
        return frame

    source = frame["Date"] if "Date" in frame.columns else frame.index
    # Compare dates, not instants. A tz-aware index becomes 04:00 once it is
    # converted to UTC, which silently fails a <= midnight test on the same day.
    mask = _as_naive_dates(source).dt.normalize() <= pd.Timestamp(settled).normalize()

    # Positional: a mask built from the index carries its own index labels, which
    # pandas then tries to align them and fails. Values sidestep that.
    mask = np.asarray(mask, dtype=bool)
    kept = frame[mask]
    dropped = len(frame) - len(kept)

    if cache_path and len(kept):
        try:
            newest = _as_naive_dates(kept["Date"] if "Date" in kept.columns else kept.index).iloc[
                -1
            ]
            if os.path.exists(cache_path):
                written = _dt.datetime.fromtimestamp(os.path.getmtime(cache_path), _dt.UTC)
                if written < _settle_instant(newest, symbol):
                    before = len(kept)
                    kept = kept[
                        _as_naive_dates(kept["Date"] if "Date" in kept.columns else kept.index)
                        < pd.Timestamp(str(newest)[:10])
                    ]
                    dropped += before - len(kept)
        except ExchangeUnknown:
            raise
        except Exception as exc:
            raise RuntimeError(f"cache freshness check failed for {symbol!r}: {exc}") from exc

    if dropped:
        through = None
        if len(kept):
            series = _as_naive_dates(kept["Date"] if "Date" in kept.columns else kept.index)
            through = str(pd.Timestamp(series.iloc[-1]).date())
        REPORT.append({"symbol": symbol, "what": what, "dropped": dropped, "kept_through": through})
    if len(kept) == 0:
        raise NoSettledBar(
            "{}: every row between the request and {} was still unsettled at {}; "
            "refusing to return an in-progress bar".format(
                symbol, str(settled)[:10], _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            )
        )
    return kept


def _cache_path(symbol):
    from tradingagents.dataflows.config import get_config
    from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component

    safe = safe_ticker_component(normalize_symbol(symbol))
    return os.path.join(get_config()["data_cache_dir"], f"{safe}-YFin-data.csv")


DEGRADED = []


def cache_needs_refresh(cache_path, newest_date, symbol) -> bool:
    """True when the cache was written before its newest bar could settle.

    Split out so it can be tested against a chosen clock. Through the real
    load path the window is narrow: upstream already refetches when the file's
    machine-local date differs from today, so a pre-close write only survives
    within the same local day.
    """
    if not cache_path or not os.path.exists(cache_path):
        return False
    written = _dt.datetime.fromtimestamp(os.path.getmtime(cache_path), _dt.UTC)
    return written < _settle_instant(newest_date, symbol)


def _refresh_cache_frame(symbol, as_of_date, cache_path, orig_load, fill_gaps=True):
    """Drop a mid-session cache and fetch again, so the answer is the close
    rather than the price that happened to be current when the file was written.

    ``fill_gaps`` is the caller's, not a default. The snapshot path asks for
    the frame exactly as the vendor reported it, and handing it carried-forward
    values instead would answer a different question.

    On failure this raises. A cache written before its own bar settled holds a
    value that is not a close, and serving it is worse than serving nothing --
    it looks like an answer. The router turns the raise into one clean
    unavailable signal; the reason is left in DEGRADED for the verdict.
    """
    from tradingagents.dataflows.errors import VendorUnavailableError

    def refuse(reason):
        DEGRADED.append({"symbol": symbol, "stage": "refresh", "error": reason})
        raise VendorUnavailableError(
            f"{symbol}: cache was written before the close and the refresh failed -- {reason}. "
            "Refusing to serve mid-session values."
        )

    try:
        os.remove(cache_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        refuse(f"could not remove the stale cache: {exc}")

    try:
        fresh = orig_load(symbol, as_of_date, fill_gaps=fill_gaps)
    except Exception as exc:
        refuse(f"{type(exc).__name__}: {exc}")

    if fresh is None or getattr(fresh, "empty", True):
        refuse("the refetch came back empty")

    REPORT.append(
        {
            "symbol": symbol,
            "what": "load_ohlcv",
            "dropped": 0,
            "kept_through": None,
            "note": "refreshed a pre-close cache",
            "fill_gaps": fill_gaps,
        }
    )
    return fresh


def filter_csv_text(text: str, symbol: str, *, end_date=None):
    """Filter the formatted CSV that get_YFin_data_online returns.

    That entry point does not return a frame; it returns a comment header
    followed by the frame as CSV text, with the trading day in the index
    column. Filtering the string rather than the frame is what the caller
    actually receives, so this is the same path the report is built from.
    """
    import io

    lines = text.splitlines()
    head = [ln for ln in lines if ln.startswith("#")]
    body = [ln for ln in lines if not ln.startswith("#")]
    if not body:
        raise NoSettledBar(f"{symbol}: vendor returned no rows to filter")

    frame = pd.read_csv(io.StringIO("\n".join(body)), index_col=0)
    kept = filter_frame(frame, symbol, what="get_YFin_data_online")

    index = _as_naive_dates(kept.index)
    last = str(pd.Timestamp(index.iloc[-1]).date())
    if end_date is not None and str(last) > str(end_date):
        raise NoSettledBar(f"{symbol}: {last} is later than the requested end {end_date}")

    # Keep the header honest: the count and the end date must describe what is
    # actually in the file, not what was asked for.
    out_head = []
    for line in head:
        if line.lower().startswith("# total records:"):
            out_head.append(
                f"# Total records: {len(kept):d} (through {last!s}, last settled session)"
            )
        elif line.lower().startswith("# stock data for"):
            out_head.append(line.rsplit(" to ", 1)[0] + f" to {last} (last settled)")
        else:
            out_head.append(line)
    return "\n".join(out_head + [""]) + kept.to_csv()


def install() -> bool:
    from tradingagents.dataflows.vendors.yahoo import market, ohlcv, snapshot

    if getattr(ohlcv, "_exchange_settle_patched", False):
        return False

    orig_load = ohlcv.load_ohlcv

    def load_ohlcv(symbol, as_of_date, fill_gaps=True, **kw):
        # The native loader and this guard share a reentrant cache lock. Keep
        # refresh/removal in the same transaction as the initial read.
        with ohlcv.cache_lock(symbol):
            return load_locked(symbol, as_of_date, fill_gaps=fill_gaps, **kw)

    def load_locked(symbol, as_of_date, fill_gaps=True, **kw):
        frame = orig_load(symbol, as_of_date, fill_gaps=fill_gaps, **kw)
        if frame is None or getattr(frame, "empty", True):
            return frame
        cache_path = _cache_path(symbol)
        # A cache written before its newest bar could settle holds a mid-flight
        # value. Dropping the row would leave yesterday's price silently; going
        # back to the vendor is what actually answers the question.
        index = _as_naive_dates(frame["Date"] if "Date" in frame.columns else frame.index)
        if cache_needs_refresh(cache_path, index.iloc[-1].date(), symbol):
            fresh = _refresh_cache_frame(symbol, as_of_date, cache_path, orig_load, fill_gaps)
            return filter_frame(fresh, symbol, what="load_ohlcv")
        return filter_frame(frame, symbol, cache_path=cache_path, what="load_ohlcv")

    ohlcv.load_ohlcv = load_ohlcv
    market.load_ohlcv = load_ohlcv
    snapshot.load_ohlcv = load_ohlcv

    orig_online = market.get_YFin_data_online

    def get_YFin_data_online(symbol, start_date, end_date, *a, **kw):
        text = orig_online(symbol, start_date, end_date, *a, **kw)
        if isinstance(text, str):
            return filter_csv_text(text, symbol, end_date=end_date)
        if text is None or getattr(text, "empty", True):
            return text
        kept = filter_frame(text, symbol, what="get_YFin_data_online")
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date) + pd.Timedelta(days=1)
        index = _as_naive_dates(kept["Date"] if "Date" in kept.columns else kept.index)
        kept = kept[(index >= start) & (index < end)]
        if getattr(kept, "empty", True):
            raise NoSettledBar(f"{symbol}: nothing settled between {start_date} and {end_date}")
        return kept

    market.get_YFin_data_online = get_YFin_data_online

    # The router imports its callable before this runtime filter is installed.
    # Update the actual vendor dispatch entry, not only the module attribute.
    from tradingagents.dataflows import router

    if router.VENDOR_METHODS["get_stock_data"]["yfinance"] is orig_online:
        router.VENDOR_METHODS["get_stock_data"]["yfinance"] = get_YFin_data_online

    orig_closes = market.get_closes

    def get_closes(symbol, start_date, end_date, *a, **kw):
        series = orig_closes(symbol, start_date, end_date, *a, **kw)
        if series is None or len(series) == 0:
            return series
        kept = filter_frame(series.to_frame(), symbol, what="get_closes")
        name = series.name or "Close"
        return kept[name] if name in kept.columns else kept.iloc[:, 0]

    market.get_closes = get_closes

    from tradingagents.memory import settlement

    if settlement.get_closes is orig_closes:
        settlement.get_closes = get_closes

    ohlcv._exchange_settle_patched = True
    return True
