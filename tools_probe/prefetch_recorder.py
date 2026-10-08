r"""Record what the Sentiment Analyst fetches for itself, once per attempt.

The sentiment node pre-fetches news, StockTwits and Reddit straight into the
prompt, so none of it produces a [Tool Call] line and a ledger built on tool
calls decides the analyst never asked for anything.

Three things this gets right:

* Rebinds only the name in sentiment_analyst. get_news is one shared tool
  object that news_analyst also imports, so wrapping the object would credit
  the sentiment analyst for the news analyst's calls.
* Wraps the original every time. A second install() re-wraps the first wrapper
  unless the original is remembered, and the first attempt's log then collects
  the second attempt's data as well.
* Decides refusal by shape, not by keyword. The vendors say "unavailable" by
  returning a placeholder -- <stocktwits unavailable: ...>,
  DATA_UNAVAILABLE: ... -- and a headline such as "Apple failed to meet analyst
  earnings expectations" is ordinary content, not a failure.

Writes, one line per fetch:
    HH:MM:SS [Data] sentiment::get_news status=ok|refused|unknown reason=...
"""

from __future__ import annotations

import datetime as _dt
import pathlib
import re

# A refusal is the value BEING a placeholder, never merely mentioning one.
_PLACEHOLDER = re.compile(r"^\s*<[^>]*>\s*$", re.DOTALL | re.MULTILINE)
_PREFIXED = re.compile(r"^\s*(?:DATA_UNAVAILABLE|NO_DATA_AVAILABLE|VENDOR_UNAVAILABLE)\s*:", re.I)

# Yahoo has no marker on its empty case; it returns one templated sentence:
#   "No news found for AAPL between 2026-09-29 and 2026-10-06"
# Anchored end to end, so a headline that merely contains the words is not
# caught by it. A report saying "no news found for the region" is not a quote.
_NO_NEWS = re.compile(
    r"^\s*No news (?:articles )?found for .+? between \d{4}-\d{2}-\d{2} "
    r"and \d{4}-\d{2}-\d{2}\s*$",
    re.IGNORECASE,
)

# Reddit writes one block per subreddit, in two shapes:
#
#   empty     r/stocks: <no posts found mentioning AAPL>
#   populated r/investing — 2 recent posts mentioning AAPL:
# Only the empty form ends at a colon, so matching the colon alone never sees a
# subreddit that returned real posts, which made a mixed result read as empty.
# Horizontal whitespace only after the colon: \s* would swallow the newline and
# fold the next subreddit into this section body.
_SUBREDDIT = re.compile(
    r"^[ \t]*r/(?P<sub>[A-Za-z0-9_+-]+)[ \t]*"
    r"(?:(?::[ \t]*(?P<inline>[^\n]*))"
    r"|—[ \t]*(?P<count>\d+)[ \t]+recent posts\b[^\n]*)$",
    re.MULTILINE,
)

STATUS_OK = "ok"
STATUS_REFUSED = "refused"
STATUS_UNKNOWN = "unknown"


def classify(text):
    """(status, reason) for one fetched value.

    Only structural signals count. Free-text keywords are how a real headline
    about a company missing its numbers got recorded as a vendor failure.
    """
    if text is None:
        return STATUS_UNKNOWN, "no value returned"
    if not isinstance(text, str):
        return STATUS_UNKNOWN, "not text"
    body = text.strip()
    if not body:
        return STATUS_REFUSED, "empty"
    if _PREFIXED.match(body) or _PLACEHOLDER.match(body):
        return STATUS_REFUSED, "vendor placeholder"
    if _NO_NEWS.match(body):
        return STATUS_REFUSED, "no news for this window"

    sections = list(_SUBREDDIT.finditer(body))
    if sections:
        # A populated block announces its own count; an empty one carries a
        # placeholder or nothing. One populated block is enough to have data.
        populated = [m for m in sections if m.group("count")]
        bodies = [m.group("inline") for m in sections if m.group("inline") is not None]
        filled = [b for b in bodies if b.strip() and not _PLACEHOLDER.match(b)]
        if not populated and not filled:
            return STATUS_REFUSED, "no posts in any subreddit"
    return STATUS_OK, ""


class PrefetchLog:
    """One recorder over one file. No state is shared between instances."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A fresh file. Truncating a path an earlier attempt still points at
        # would erase that attempt's evidence.
        self.path.write_text("", encoding="utf-8")
        self.lines = 0

    def record(self, owner, method, text):
        status, reason = classify(text)
        if status == STATUS_OK:
            body = "{}::{} status=ok detail={}".format(owner, method, " ".join(text.split())[:90])
        else:
            body = f"{owner}::{method} status={status} reason={reason}"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{} [Data] {}\n".format(_dt.datetime.now().strftime("%H:%M:%S"), body))
        self.lines += 1


def _original(tool):
    """The pre-install object, however many times we have wrapped it."""
    seen = 0
    while getattr(tool, "_prefetch_original", None) is not None and seen < 20:
        tool = tool._prefetch_original
        seen += 1
    return tool


class _ToolProxy:
    """Stands in for a StructuredTool inside one module only."""

    def __init__(self, tool, log, method):
        object.__setattr__(self, "_prefetch_original", tool)
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_method", method)

    def _wrapped(self, *a, **kw):
        result = self._prefetch_original.func(*a, **kw)
        self._log.record("sentiment", self._method, result)
        return result

    def __call__(self, *a, **kw):
        return self._prefetch_original(*a, **kw)

    def __getattr__(self, name):
        if name == "func":
            # The node reaches the vendor through .func; that is the seam to wrap.
            return object.__getattribute__(self, "_wrapped")
        return getattr(object.__getattribute__(self, "_prefetch_original"), name)


def _function_wrapper(fn, log, method):
    def wrapped(*a, **kw):
        result = fn(*a, **kw)
        log.record("sentiment", method, result)
        return result

    wrapped._prefetch_original = fn
    return wrapped


def install(log_path):
    """Bind the three pre-fetched sources to a new recorder. Returns it or None.

    Always wraps what was there before the first install, so a second install
    targets only its own file instead of chaining onto the first recorder.
    """
    module = None
    for dotted in (
        "tradingagents.agents.analysts.sentiment_analyst",
        "tradingagents.agents.sentiment_analyst",
    ):
        try:
            module = __import__(dotted, fromlist=["*"])
            break
        except Exception:
            continue
    if module is None:
        return None

    log = PrefetchLog(log_path)

    tool = _original(getattr(module, "get_news", None))
    if tool is not None and hasattr(tool, "func"):
        module.get_news = _ToolProxy(tool, log, "get_news")

    for name, method in (
        ("fetch_stocktwits_messages", "get_stocktwits"),
        ("fetch_reddit_posts", "get_reddit"),
    ):
        fn = _original(getattr(module, name, None))
        if callable(fn):
            setattr(module, name, _function_wrapper(fn, log, method))

    return log
