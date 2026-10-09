r"""Accept or reject one analysis by its own contents.

What this deliberately refuses to be fooled by, because each of them has
happened here: an exit code of 0 while a stage was dropped; a report file
that exists but holds nothing but a repeated role header; a final rating that
came out fine while two of three risk agents were empty.

Three rules follow from that:

* The report directory is an argument. It is never discovered by looking for
  the newest thing on disk -- that is how a failed run gets graded against the
  previous run's leftovers.
* The analysts come from the report's own ``run_settings``. A checker told
  only "market" would pass a run that was asked for four.
* A missing file is a failure, never a skip, and a stage with no body is a
  failure even when the file is non-empty.

Exit code 1 rejects incomplete stages/data; 2 requests financial content review.
Exit code 0 establishes completeness only, never full factual correctness.

    .venv\Scripts\python.exe tools_probe\check_run_complete.py <report-dir> [--json]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re

# A body shorter than this is a stub such as "Bull Analyst: ".
MIN_BODY_CHARS = 200

# A line that is only a role label, e.g. "Bull Analyst:" or "**Risk Analyst**：".
ROLE_LINE = re.compile(
    r"^\s*(?:[-*>#\u2022]\s*)?(?:\**)?([A-Z][A-Za-z \u4e00-\u9fff]{2,40}?)"
    r"(?:\**)?\s*[:\uff1a]\s*(?:\**)?\s*$"
)

# The CLI calls the sentiment analyst "social"; the state log records the
# enum value "sentiment". Both name the same saved report.
ANALYST_FILES = {
    "market": "1_analysts/market.md",
    "news": "1_analysts/news.md",
    "social": "1_analysts/sentiment.md",
    "sentiment": "1_analysts/sentiment.md",
    "fundamentals": "1_analysts/fundamentals.md",
}

STAGES = {
    "research_manager": ("2_research/manager.md", "Research Manager"),
    "bull": ("2_research/bull.md", "Bull Researcher"),
    "bear": ("2_research/bear.md", "Bear Researcher"),
    "trader": ("3_trading/trader.md", "Trader"),
    "aggressive": ("4_risk/aggressive.md", "Aggressive Risk Analyst"),
    "conservative": ("4_risk/conservative.md", "Conservative Risk Analyst"),
    "neutral": ("4_risk/neutral.md", "Neutral Risk Analyst"),
    "decision": ("5_portfolio/decision.md", "Portfolio Manager"),
}


def body_of(text: str) -> str:
    """Drop blank lines and lines that are only a role label."""
    # A program-generated review note must not rescue an empty/short model reply.
    text = re.split(r"<!-- financial-semantics: (?:REVIEW|LIMITED_CHECK) -->", text, maxsplit=1)[0]
    return "\n".join(
        line for line in text.splitlines() if line.strip() and not ROLE_LINE.match(line)
    ).strip()


def enabled_analysts(report_dir: pathlib.Path, fallback: list[str]) -> list[str]:
    """Read the analysts the run says it used.

    The saved report carries them in the state log; its header only says
    "Analysts: market, social, news" in prose, which is good enough to parse
    and does not depend on a state log being present.
    """
    for candidate in report_dir.rglob("full_states_log_*.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        runs = data if isinstance(data, list) else [data]
        if not runs:
            continue
        settings = runs[-1].get("run_settings") or {}
        found = settings.get("analysts")
        if found:
            return [str(a).strip() for a in found if str(a).strip()]
    header = report_dir / "complete_report.md"
    if header.exists():
        for line in header.read_text(encoding="utf-8", errors="replace").splitlines()[:12]:
            match = re.match(r"^-\s*Analysts:\s*(.+?)\s*;", line)
            if match:
                names = [n.strip() for n in match.group(1).split(",") if n.strip()]
                if names:
                    return names
    return fallback


def repeated_role_header(raw: str) -> bool:
    """True when the file is one role label echoed back over and over.

    "Bull Analyst: Bull Analyst: " is not a role line by the strict reading,
    because there is text after the colon. But the text is the label again, so
    reporting it as merely "body too short" hides what actually happened.
    """
    pieces = [p.strip().strip("*").strip() for p in re.split(r"[:\uff1a]", raw)]
    pieces = [p for p in pieces if p]
    if len(pieces) < 2:
        return False
    return len(set(pieces)) == 1


# Three outcomes per data request, and the difference matters when a report is
# read back. A stage full of prose is not a stage with evidence.
#   ok        -- the vendor returned content
#   refused   -- it returned nothing: unavailable, or withheld by a rule
#   unknown   -- a call with no recorded response, or a stage restored from a
#                checkpoint whose original responses are not in this log
# Never inferred from the absence of an error: a restored stage produces no
# errors here and is still unevidenced.
OK, REFUSED, UNKNOWN = "ok", "refused", "unknown"

# Which analyst owns which request. A call is attributed by the method it made,
# not by proximity, so two analysts asking for the same thing both count.
TOOL_OWNER = {
    "get_stock_data": "market",
    "get_indicators": "market",
    "get_verified_market_snapshot": "market",
    "get_news": "news",
    "get_global_news": "news",
    "get_macro_indicators": "news",
    "get_prediction_markets": "news",
    "get_stocktwits": "sentiment",
    "get_reddit": "sentiment",
    "get_fundamentals": "fundamentals",
    "get_balance_sheet": "fundamentals",
    "get_cashflow": "fundamentals",
    "get_income_statement": "fundamentals",
    "get_insider_transactions": "fundamentals",
}
KEY_ANALYSTS = ("market", "news", "sentiment", "fundamentals")

_CALL = re.compile(r"^\s*\d\d:\d\d:\d\d\s*\[Tool Call\]\s*([a-z_]+)\s*\(")
_NO_DATA = re.compile(r"NO_DATA_AVAILABLE", re.IGNORECASE)
_UNAVAILABLE = re.compile(r"DATA_UNAVAILABLE", re.IGNORECASE)
_WITHHELD = re.compile(r"(?:is|are) withheld for this date", re.IGNORECASE)
_SERVE = re.compile(r"serve (get_[a-z_]+)", re.IGNORECASE)
_RESUMED = re.compile(r"Resuming the saved run", re.IGNORECASE)

_CATEGORY_NAMES = (
    "Company Fundamentals",
    "Profile fundamentals",
    "Balance Sheet",
    "Cash Flow",
    "Income Statement",
    "Insider Transactions",
)
# No word boundary here: one written through a shell arrives as a backspace.
_CATEGORY = re.compile(r"#\s*(" + "|".join(_CATEGORY_NAMES) + r")(?![A-Za-z])", re.IGNORECASE)


def _lines_of(tool_log):
    if tool_log is None:
        return []
    reader = getattr(tool_log, "read", None)
    if callable(reader):
        return reader()
    path = pathlib.Path(tool_log)
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


# What a successful [Data] line actually is, when the log does not say which
# call it answers. Deliberately narrow: anything unrecognised is attributed to
# nobody rather than guessed into the wrong analyst.
_DELIVERY = [
    ("Global Market News", "news"),
    ("## FRED", "news"),
    ("Macro", "news"),
    ("# Stock data for", "market"),
    ("# Verified market snapshot", "market"),
]


# The pre-fetch recorder states the outcome outright:
#     owner::method status=ok|refused|unknown reason=...
# It is the only place that knows whether an empty return was a refusal or
# simply an absence it could not classify, so it is read, not inferred.
_PREFETCH = re.compile(
    r"\b(sentiment|news|market|fundamentals)::(get_[a-z_]+)\s+status=(ok|refused|unknown)"
    r"(?:\s+reason=([^\n]*))?"
)


def prefetch_line(line):
    """(owner, method, status, reason) when the line is a recorded pre-fetch."""
    m = _PREFETCH.search(line)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), (m.group(4) or "").strip()


def _owner_of(line, pending):
    # An explicit owner tag wins over anything inferred from the text. The
    # pre-fetch recorder writes "sentiment::get_news: ..." precisely because
    # get_news alone is ambiguous: the news analyst and the sentiment analyst
    # both fetch it, and content sniffing cannot tell them apart.
    tagged = re.search(r"\b(sentiment|news|market|fundamentals)::(get_[a-z_]+)", line.lower())
    if tagged:
        return tagged.group(1)
    text = line.lower()
    for needle, owner in _DELIVERY:
        if needle.lower() in text:
            return owner
    # An OHLCV dump: column headers, or a date plus an open/high/low/close set.
    if re.search(r"open", text) and re.search(r"close", text):
        return "market"
    for name, owner in TOOL_OWNER.items():
        if name in text:
            return owner
    return "unknown"


def data_ledger(tool_logs):
    """Per request: ok, refused or unknown -- and what each analyst got.

    Accepts one log or a list of them. The framework log is not the whole
    story: the Sentiment Analyst pre-fetches news, StockTwits and Reddit
    directly inside its node, before the model is called, so none of those
    three ever appear as a tool call. Judging that analyst on tool calls alone
    makes it look like it asked for nothing. The pre-fetch recorder writes its
    findings in the same [Data] shape so they land here too.
    """
    # A RunLog is neither a str nor a Path, so check for the reader first:
    # anything with .read() is a single source, everything else is a list.
    if (
        tool_logs is None
        or isinstance(tool_logs, (str, pathlib.Path))
        or hasattr(tool_logs, "read")
    ):
        tool_logs = [tool_logs]
    lines = []
    for source in tool_logs:
        lines.extend(_lines_of(source))

    entries = {}
    pending = {}  # call -> already answered
    resumed = False

    def entry(key, owner):
        return entries.setdefault(
            key,
            {
                "key": key,
                "owner": owner,
                "ok": 0,
                "refused": 0,
                "unknown": 0,
                "reason": "",
                "sample": "",
            },
        )

    def note_ok(key, owner, sample):
        rec = entry(key, owner)
        rec["ok"] += 1
        if not rec["sample"]:
            rec["sample"] = sample

    def note_refused(key, owner, reason, sample):
        rec = entry(key, owner)
        rec["refused"] += 1
        if not rec["reason"]:
            rec["reason"] = reason
            rec["sample"] = sample

    for line in lines:
        if _RESUMED.search(line):
            resumed = True
            continue
        if "[Data]" not in line:
            call = _CALL.match(line)
            if call:
                pending.setdefault(call.group(1), False)
            continue

        # A request answered by this line is no longer outstanding. Without
        # this, a batch of five calls followed by five replies leaves every one
        # of them outstanding and each is reported as unproven at the end.
        pending.clear()

        pre = prefetch_line(line)
        if pre is not None:
            owner, method, status, reason = pre
            if status == "ok":
                note_ok(f"{owner}::{method}", owner, line.strip()[:120])
            elif status == "refused":
                note_refused(f"{owner}::{method}", owner, reason or "refused", line.strip()[:120])
            else:
                rec = entry(f"{owner}::{method}", owner)
                rec["unknown"] += 1
                if not rec["reason"]:
                    rec["reason"] = reason or "not classifiable"
            continue

        no_data = _NO_DATA.search(line)
        unavailable = _UNAVAILABLE.search(line)
        withheld = _WITHHELD.search(line)

        tagged = re.search(r"(sentiment|news|market|fundamentals)::(get_[a-z_]+)", line.lower())

        if no_data or unavailable:
            served = _SERVE.search(line)
            if served:
                key, owner = served.group(1), TOOL_OWNER.get(served.group(1), "unknown")
            elif no_data:
                key, owner = "symbol-wide", "market"
            elif tagged:
                key, owner = tagged.group(2), tagged.group(1)
            else:
                key, owner = "unattributed", "unknown"
            note_refused(key, owner, "no_data" if no_data else "unavailable", line.strip()[:120])
            continue
        if withheld:
            cat = _CATEGORY.search(line.split("withheld", 1)[0])
            note_refused(
                cat.group(1) if cat else "withheld", "fundamentals", "withheld", line.strip()[:120]
            )
            continue

        cat = _CATEGORY.search(line.split("withheld", 1)[0])
        owner = "fundamentals" if cat else _owner_of(line, pending)
        note_ok(f"delivered:{owner}", owner, line.strip()[:120])

    for name in sorted(n for n, answered in pending.items() if not answered):
        note_refused(name, TOOL_OWNER.get(name, "unknown"), "no response recorded", "")

    if resumed:
        # A replayed stage was produced by an earlier attempt. Its evidence is
        # not in these logs, so it cannot be assumed good -- and a later
        # success elsewhere does not stand in for it either.
        entries["<restored from checkpoint>"] = {
            "key": "<restored from checkpoint>",
            "owner": "*",
            "ok": 0,
            "refused": 0,
            "unknown": 1,
            "reason": "stage restored; its original responses are not in this run",
            "sample": "",
        }

    rollup = {}
    for a in KEY_ANALYSTS:
        mine = [e for e in entries.values() if e["owner"] == a]
        ok = sum(e["ok"] for e in mine)
        refused = sum(e["refused"] for e in mine)
        unknown = sum(e["unknown"] for e in mine)
        if not mine:
            rollup[a] = {"state": "no-evidence", "ok": 0, "refused": 0, "unknown": 0}
        elif ok > 0:
            rollup[a] = {
                "state": "partial" if refused or unknown else "ok",
                "ok": ok,
                "refused": refused,
                "unknown": unknown,
            }
        elif unknown and not refused:
            rollup[a] = {"state": "unproven", "ok": 0, "refused": 0, "unknown": unknown}
        else:
            rollup[a] = {"state": "none", "ok": 0, "refused": refused, "unknown": unknown}
    return {"entries": list(entries.values()), "rollup": rollup, "resumed": resumed}


def data_gaps(tool_logs):
    """The refused subset, for callers that only want the failures."""
    return [e for e in data_ledger(tool_logs)["entries"] if e["refused"]]


def analyst_data_state(tool_logs):
    return data_ledger(tool_logs)["rollup"]


def usable_for_complete_analysis(rollup, analysts, resumed):
    """Key analysts with nothing to stand on, so this is not a full analysis.

    A rating in the report is not evidence: a run with no prices still prints
    Sell. Only recorded data counts. When a checkpoint was replayed, the stages
    it restored carry their own separate record, and any analyst that depends
    on them is unproven unless this run also delivered its own data.
    """
    wanted = {a.lower() for a in analysts}
    bad = []
    for name in KEY_ANALYSTS:
        if name not in wanted:
            continue
        state = (rollup.get(name) or {}).get("state")
        if state in ("none", "unproven", "no-evidence"):
            bad.append(name)
    if resumed:
        # A replayed stage was produced by an earlier attempt, and its original
        # responses are not in these logs. Nothing here can prove the restored
        # work stands on real data, so the run is not accepted -- a later success
        # elsewhere does not stand in for it. Rejecting is the reliable half;
        # reconstructing the missing evidence is not attempted yet.
        bad.append("<restored stages: no original evidence in this run>")
    return bad


def check(report_dir: pathlib.Path, fallback_analysts=None, tool_log=None, degraded=None) -> dict:
    if not report_dir.is_dir():
        return {
            "ok": False,
            "results": [],
            "reason": f"report directory does not exist: {report_dir}",
        }
    analysts = enabled_analysts(report_dir, fallback_analysts or [])
    results = []
    for key in analysts:
        rel = ANALYST_FILES.get(key)
        if rel:
            results.append(judge(report_dir / rel, "Analyst: " + key))
        else:
            results.append(
                {
                    "stage": "Analyst: " + key,
                    "file": "-",
                    "ok": False,
                    "reason": "no file mapping for this analyst",
                    "raw_bytes": 0,
                    "body_chars": 0,
                }
            )
    for rel, label in STAGES.values():
        results.append(judge(report_dir / rel, label))
    failed = [r for r in results if not r["ok"]]
    # Two separate questions. "Did the stage produce text" and "did the stage
    # have data" have different answers: a fundamentals report can be long,
    # well written, and entirely about the data it never received. And a run with
    # no prices still prints a rating, so a rating proves nothing.
    ledger = data_ledger(tool_log)
    rollup = ledger["rollup"]
    unproven = usable_for_complete_analysis(rollup, analysts, ledger["resumed"])
    for record in results:
        for analyst, info in rollup.items():
            if record["stage"] == "Analyst: " + analyst:
                record["data_state"] = info["state"]
    ok = not failed and not unproven
    financial_review = content_review(report_dir, analysts)
    return {
        "ok": ok,
        "content_review": financial_review,
        "analysts": analysts,
        "results": results,
        "failed": failed,
        "data_ledger": ledger,
        "data_gaps": [e for e in ledger["entries"] if e["refused"]],
        "analyst_data": rollup,
        "starved_analysts": unproven,
        "resumed": ledger["resumed"],
        "stages_ok": not failed,
        "degraded": degradation_notes(degraded),
    }


def content_review(report_dir, analysts):
    """Separate operational completeness from the analyst's bounded content screen."""
    from tradingagents.dataflows.financial_metrics import (
        FINANCIAL_CHECK_MARKER,
        FINANCIAL_REVIEW_MARKER,
    )

    if "fundamentals" not in analysts:
        return "NOT_CHECKED"
    path = pathlib.Path(report_dir) / "1_analysts" / "fundamentals.md"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if FINANCIAL_REVIEW_MARKER in text:
        return "REVIEW"
    if FINANCIAL_CHECK_MARKER in text:
        return "LIMITED_CHECK_NO_FLAGS"
    return "NOT_CHECKED"


def degradation_notes(notes):
    """What the data layer had to give up on, as caller-visible records."""
    return [dict(n) for n in (notes or [])]


def judge(path: pathlib.Path, label: str) -> dict:
    record = {"stage": label, "file": path.name}
    if not path.exists():
        record.update(ok=False, reason="missing", raw_bytes=0, body_chars=0)
        return record
    raw = path.read_text(encoding="utf-8", errors="replace")
    body = body_of(raw)
    record["raw_bytes"] = path.stat().st_size
    record["body_chars"] = len(body)
    if len(body) < MIN_BODY_CHARS:
        record["ok"] = False
        if len(body) == 0:
            record["reason"] = "role-headers-only" if len(raw.strip()) else "empty file"
        elif repeated_role_header(raw):
            record["reason"] = "role-headers-only"
        else:
            record["reason"] = f"body too short ({len(body):d} < {MIN_BODY_CHARS:d})"
    else:
        record["ok"] = True
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report_dir")
    parser.add_argument(
        "--analysts", default=None, help="only if the report does not say which analysts ran"
    )
    parser.add_argument("--tool-log", default=None, help="message_tool.log for the run")
    parser.add_argument(
        "--degraded", default=None, help="JSON list of data-layer degradation records"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    import json as _json

    report_dir = pathlib.Path(args.report_dir)
    fallback = [a.strip() for a in (args.analysts or "").split(",") if a.strip()]
    verdict = check(
        report_dir, fallback, args.tool_log, _json.loads(args.degraded) if args.degraded else None
    )

    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2, default=str))
        return 2 if verdict.get("content_review") == "REVIEW" else (0 if verdict["ok"] else 1)

    if not verdict.get("results"):
        print("REJECT: {}".format(verdict.get("reason")))
        return 1
    print(f"report   : {report_dir}")
    print("analysts : {} (as recorded by the run)".format(",".join(verdict["analysts"])))
    print("-" * 72)
    for r in verdict["results"]:
        mark = "ok  " if r["ok"] else "FAIL"
        extra = "" if r["ok"] else "  <- {}".format(r.get("reason", ""))
        tag = "" if not r.get("data_state") else "  data={}".format(r["data_state"])
        print(
            f"  [{mark}] {r['stage']:<28} raw={r['raw_bytes']:6d} "
            f"body={r['body_chars']:6d}{tag}{extra}"
        )

    total = len(verdict["results"])
    failed = verdict["failed"]
    rollup = verdict.get("analyst_data") or {}
    unproven = verdict.get("starved_analysts") or []
    ledger = verdict.get("data_ledger") or {"entries": []}

    # Three claims, three lines. A stage that produced text is not the same as a
    # stage that had data, and a rating printed in the report is evidence of
    # neither: a run with no prices still prints Sell.
    print("-" * 72)
    print(f"STAGES  : {total - len(failed):d}/{total:d} complete")
    for a in sorted(rollup):
        i = rollup[a]
        print(
            "DATA    : {!s:<12} {!s:<11} ok={:d} refused={:d} unknown={:d}".format(
                a, i["state"], i["ok"], i["refused"], i["unknown"]
            )
        )
    if verdict.get("resumed"):
        print("RESUMED : a checkpoint was replayed; restored stages carry no evidence here")
    print("VERDICT : {!s}".format("ACCEPT" if verdict["ok"] else "REJECT"))
    print("CONTENT : " + verdict.get("content_review", "NOT_CHECKED"))
    if unproven:
        print(
            "          {} had no usable input; this is NOT a complete analysis".format(
                ", ".join(unproven)
            )
        )

    refused = [e for e in ledger.get("entries", []) if e["refused"]]
    unknown = [e for e in ledger.get("entries", []) if e["unknown"]]
    if refused or unknown:
        print("-" * 72)
        print("DATA REQUESTS, this run only")
        for e in sorted(refused, key=lambda x: x["key"]):
            print(
                "   refused  {!s:<26} {!s:<11} x{:d} {!s}".format(
                    e["key"], e["reason"], e["refused"], e["sample"][:40]
                )
            )
        for e in sorted(unknown, key=lambda x: x["key"]):
            print("   unknown  {!s:<26} {!s:<11} x{:d}".format(e["key"], e["reason"], e["unknown"]))
    for d in verdict.get("degraded") or []:
        print(
            "   degraded {!s:<22} {!s:<8} {!s}".format(
                d.get("symbol"), d.get("stage"), str(d.get("error"))[:44]
            )
        )
    return 2 if verdict.get("content_review") == "REVIEW" else (0 if verdict["ok"] else 1)
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
