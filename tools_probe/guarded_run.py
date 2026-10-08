r"""Run an analysis with the empty-response guardrails in place.

Three things this does that a bare `cli.main` run does not.

1.  --effort LEVEL   Sends ``reasoning_effort`` for models the framework
    filters it out of. ``_supports_reasoning_effort`` in openai_client.py only
    matches native OpenAI model names (gpt-5+, o-series), so a MiniMax model
    silently loses the kwarg even though the endpoint honours it. Measured on
    this gateway: reasoning tokens 1002 -> 147 at level low.

2.  Instrumentation. Every LLM call is appended to data/probe/llm-calls.jsonl
    with the node it belongs to, the round within that node, the tools that
    were bound, the tool_choice actually sent, finish_reason, body length,
    tool_calls, and token usage split into visible and reasoning.

3.  Acceptance. After the run, every enabled stage is checked for real content
    (see check_run_complete.py). A run that fails acceptance is retried, up to
    --max-retries, and the final verdict is the exit code. It does not report
    success for a run that quietly dropped a stage.

    .venv\Scripts\python.exe tools_probe\guarded_run.py \\
        --effort low --max-retries 2 -- \\
        --ticker AAPL --date 2026-10-05 --analysts market --save --no-show

Everything after ``--`` goes to the CLI unchanged.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBE_DIR = REPO / "data" / "probe"
PROBE = PROBE_DIR / "llm-calls.jsonl"  # current run; history lives beside it


def start_log(tag: str) -> pathlib.Path:
    """Open a fresh log for this run and leave earlier ones in place.

    Overwriting one file loses the baseline a comparison is measured against,
    so each run gets its own and the summary only ever reads the current one.
    """
    import datetime as _dt

    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = PROBE_DIR / (f"{tag}-{stamp}.jsonl")
    path.write_text("", encoding="utf-8")
    return path


# Distinctive phrases in each agent's system prompt -> node name. Used to label
# calls; the framework does not put the node name on the LLM run.
# Prompts follow TRADINGAGENTS_OUTPUT_LANGUAGE, so both scripts have to be
# listed. Order matters: the first hit wins, and the more specific debate
# roles are listed before the ones they quote.
NODE_MARKERS = [
    ("research_manager", ("research manager", "debate manager", "研究经理", "辩论经理")),
    ("portfolio_manager", ("portfolio manager", "组合经理", "投资组合经理")),
    ("aggressive", ("aggressive risk", "aggressive analyst", "激进")),
    ("conservative", ("conservative risk", "conservative analyst", "保守")),
    ("neutral", ("neutral risk", "neutral analyst", "中立")),
    ("bull", ("bull analyst", "bullish researcher", "多头")),
    ("bear", ("bear analyst", "bearish researcher", "空头")),
    ("trader", ("trader agent", "you are a trader", "交易员")),
    ("fundamentals", ("fundamentals analyst", "基本面分析师")),
    ("sentiment", ("sentiment analyst", "情绪分析师")),
    ("news", ("news analyst", "新闻分析师")),
    ("market", ("market analyst", "市场分析师")),
]


def sniff_node(messages) -> str:
    blob = ""
    for message in messages or []:
        content = getattr(message, "content", None)
        if content is None and isinstance(message, (tuple, list)) and len(message) > 1:
            content = message[1]
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(getattr(b, "text", ""))
                for b in content
            )
        blob += str(content or "").lower()[:4000]
    for name, markers in NODE_MARKERS:
        if any(marker in blob for marker in markers):
            return name
    return "unknown"


_EFFORT_FAMILIES = re.compile(r"^(?:minimax|deepseek|qwen|glm|moonshot|zhipu|dashscope)", re.I)


def enable_effort() -> bool:
    """Let reasoning_effort reach models the framework filters it out of.

    ``_supports_reasoning_effort`` exists so a native OpenAI request does not
    400 on a model that has no reasoning support. That rule is OpenAI's, and it
    is being applied to every provider in the OpenAI-compatible family. These
    vendors take the kwarg themselves, so the filter is what drops it.
    """
    from tradingagents.llm_clients import factory, openai_client as oc

    # Barrier 1: build_llm_kwargs only forwards the kwarg when
    # provider == "openai"; "openai_compatible" never gets one at all.
    original_build = factory.build_llm_kwargs

    def patched_build(config):
        kwargs = original_build(config)
        effort = config.get("openai_reasoning_effort")
        provider = (config.get("llm_provider") or "").lower()
        if effort and provider not in ("openai",) and "reasoning_effort" not in kwargs:
            model = config.get("quick_think_llm") or config.get("deep_think_llm") or ""
            if _EFFORT_FAMILIES.match(model.strip()):
                kwargs["reasoning_effort"] = effort
        return kwargs

    factory.build_llm_kwargs = patched_build

    # Barrier 2: the passthrough loop drops it unless the model name looks
    # like a native OpenAI reasoning model.
    original_filter = oc._supports_reasoning_effort

    def patched_filter(model: str) -> bool:
        if original_filter(model):
            return True
        return bool(_EFFORT_FAMILIES.match(model.strip()))

    oc._supports_reasoning_effort = patched_filter
    return True


def install_probe():
    from tradingagents.llm_clients.openai_client import LocalCompatibleChatOpenAI

    original_generate = LocalCompatibleChatOpenAI._generate
    original_bind = LocalCompatibleChatOpenAI.bind_tools
    ctx = {"seq": 0, "rounds": {}}
    bound = {"tools": [], "tool_choice": None}

    def patched_bind(self, tools, **kwargs):
        names = []
        for tool in tools or []:
            fn = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(fn, dict):
                names.append(fn.get("name"))
            elif hasattr(tool, "name"):
                names.append(tool.name)
            elif hasattr(tool, "__name__"):
                names.append(tool.__name__)
        bound["tools"] = [n for n in names if n]
        bound["tool_choice"] = kwargs.get("tool_choice", "<unset>")
        return original_bind(self, tools, **kwargs)

    def patched_generate(self, *args, **kwargs):
        messages = kwargs.get("messages")
        if messages is None and args:
            messages = args[0]
        node = sniff_node(messages)
        ctx["seq"] += 1
        seq = ctx["seq"]
        seen = ctx["rounds"].get(node, 0) + 1
        ctx["rounds"][node] = seen

        shape = {
            "req_max_tokens": getattr(self, "max_tokens", None),
            "req_reasoning_split": None,
            "req_payload_keys": None,
            "req_tool_choice": bound["tool_choice"],
            "req_tool_count": len(bound["tools"]),
            "req_tool_names": list(bound["tools"])[:6],
        }
        try:
            builder = getattr(self, "_get_request_payload", None)
            if builder is not None:
                payload = builder(messages or [])
                # The wire body, as built -- not what we hope was sent.
                shape["req_payload_keys"] = sorted(k for k in payload if k != "messages")
                shape["req_max_tokens"] = (
                    payload.get("max_tokens")
                    or payload.get("max_completion_tokens")
                    or shape["req_max_tokens"]
                )
                shape["req_reasoning_effort"] = payload.get("reasoning_effort")
                shape["req_reasoning_split"] = (payload.get("extra_body") or {}).get(
                    "reasoning_split"
                )
                shape["req_response_format"] = bool(payload.get("response_format"))
        except Exception:
            pass
        effort = getattr(self, "reasoning_effort", None)

        result = original_generate(self, *args, **kwargs)

        record = {
            "seq": seq,
            "node": node,
            "round": seen,
            "model": getattr(self, "model_name", None) or getattr(self, "model", None),
            "req_tools": bound["tools"][:12],
            "req_tool_choice": bound["tool_choice"],
            "req_reasoning_effort": effort,
        }
        record.update(shape)
        try:
            gens = getattr(result, "generations", None) or []
            gen = gens[0] if gens else None
            if isinstance(gen, (list, tuple)):
                gen = gen[0] if gen else None
            message = getattr(gen, "message", None) if gen is not None else None
            body = getattr(message, "content", "") if message is not None else ""
            if not isinstance(body, str):
                body = str(body)
            tool_calls = (
                list(getattr(message, "tool_calls", None) or []) if message is not None else []
            )
            info = dict(getattr(gen, "generation_info", None) or {})
            meta = dict(getattr(message, "response_metadata", None) or {})
            record["finish_reason"] = info.get("finish_reason") or meta.get("finish_reason")
            record["body_chars"] = len(body)
            record["tool_calls"] = len(tool_calls)
            record["empty"] = len(body.strip()) == 0 and not tool_calls
            usage = (
                meta.get("token_usage")
                or (getattr(result, "llm_output", None) or {}).get("token_usage")
                or {}
            )
            if usage:
                details = usage.get("completion_tokens_details") or {}
                record["usage"] = {
                    "prompt": usage.get("prompt_tokens"),
                    "completion": usage.get("completion_tokens"),
                    "total": usage.get("total_tokens"),
                    "reasoning": details.get("reasoning_tokens"),
                }
        except Exception as exc:
            record["probe_error"] = f"{type(exc).__name__}: {exc}"

        PROBE.parent.mkdir(parents=True, exist_ok=True)
        with PROBE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        # A call that carried tools has now been consumed; the next unbound call
        # (a decision) should not inherit the tool list.
        bound["tools"] = []
        bound["tool_choice"] = None
        return result

    LocalCompatibleChatOpenAI.bind_tools = patched_bind
    LocalCompatibleChatOpenAI._generate = patched_generate


def summarize() -> int:
    if not PROBE.exists():
        print("probe: no records")
        return 0
    records = [
        json.loads(line) for line in PROBE.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    records = [r for r in records if not r.get("marker")]
    empty = [r for r in records if r.get("empty")]

    # A structured call is one that forced a tool; a free-text call did not.
    # The two failures are different problems and are counted apart: a
    # structured call that comes back with no tool call is a parse failure,
    # while a free-text call with no body is a truncated reply.
    def is_structured(r):
        """A structured call binds one synthetic tool carrying the output
        schema; a data call binds the analyst's real toolset. The framework
        suppresses tool_choice for these models, so a forced choice is not a
        reliable signal -- the toolset is.
        """
        choice = r.get("req_tool_choice")
        if choice and choice != "<unset>":
            return True
        names = r.get("req_tool_names") or []
        count = r.get("req_tool_count")
        if count is None:
            return False
        if count == 1 and names:
            return True
        if count == 0:
            return False
        return False

    structured = [r for r in records if is_structured(r)]
    structured_failed = [r for r in structured if not r.get("tool_calls")]
    freetext = [r for r in records if not is_structured(r)]
    freetext_empty = [r for r in freetext if r.get("empty")]

    print()
    print("=" * 78)
    print(f"log    : {PROBE.name}")
    print(
        "calls  : {:d}   max_tokens={!s}   effort={!s}".format(
            len(records),
            sorted({r.get("req_max_tokens") for r in records if r.get("req_max_tokens")}),
            sorted({str(r.get("req_reasoning_effort")) for r in records})[:3],
        )
    )
    print(
        f"structured calls : {len(structured):d}   of which no tool call came back: {len(structured_failed):d}"
    )
    print(
        f"free-text calls  : {len(freetext):d}   of which the body was empty     : {len(freetext_empty):d}"
    )
    print("-" * 78)
    print(
        "{!s:<18} {!s:>6} {!s:>8} {!s:>8} {!s:>10} {!s:>10}".format(
            "node", "calls", "struct", "empty", "reasoning", "body"
        )
    )
    by_node = {}
    for r in records:
        by_node.setdefault(r.get("node") or "unknown", []).append(r)
    for node, rows in sorted(by_node.items(), key=lambda kv: str(kv[0])):
        tot_reason = sum((r.get("usage") or {}).get("reasoning") or 0 for r in rows)
        tot_body = sum(r.get("body_chars") or 0 for r in rows)
        print(
            "{!s:<18} {:>6d} {:>8d} {:>8d} {!s:>10} {!s:>10}".format(
                node,
                len(rows),
                sum(1 for r in rows if is_structured(r)),
                sum(1 for r in rows if r.get("empty")),
                tot_reason,
                tot_body,
            )
        )
    for label, rows in (
        ("STRUCTURED, no tool call", structured_failed),
        ("FREE-TEXT, empty body", freetext_empty),
    ):
        if not rows:
            continue
        print("-" * 78)
        print(label)
        for r in rows:
            u = r.get("usage") or {}
            print(
                "  seq={!s:<4} node={!s:<16} finish={!s:<9} completion={!s:<7} reasoning={!s:<7} tc={!s}".format(
                    r.get("seq"),
                    r.get("node"),
                    r.get("finish_reason"),
                    u.get("completion"),
                    u.get("reasoning"),
                    r.get("tool_calls"),
                )
            )
    print("=" * 78)
    return len(structured_failed) + len(freetext_empty)

    return len(empty)


def main() -> int:
    argv = sys.argv[1:]
    if "--" in argv:
        cut = argv.index("--")
        own, cli_args = argv[:cut], argv[cut + 1 :]
    else:
        own, cli_args = argv, []

    parser = argparse.ArgumentParser()
    parser.add_argument("--effort", default=None, help="reasoning_effort to send (low/medium/high)")
    parser.add_argument("--max-retries", type=int, default=0)
    parser.add_argument(
        "--analysts", default=None, help="only used if the report does not record them"
    )
    parser.add_argument(
        "--no-probe", action="store_true", help="stop recording calls; acceptance still runs"
    )
    parser.add_argument("--no-exchange-fix", action="store_true")
    parser.add_argument("--log-tag", default="run")
    args = parser.parse_args(own)

    sys.path.insert(0, str(REPO / "tools_probe"))

    if args.effort:
        import os

        os.environ["TRADINGAGENTS_OPENAI_REASONING_EFFORT"] = args.effort
        enable_effort()
        print(f"guard: reasoning_effort={args.effort} enabled")

    if not args.no_exchange_fix:
        import exchange_sessions

        if exchange_sessions.install():
            print("guard: unsettled-session bars will be dropped")

    import check_run_complete as C
    import probe_lib

    # The attempt directory is named from this, so it is needed whether or not
    # the model probe is on: --no-probe silences the probe, not the evidence.
    # A second-precision stamp is not a unique run id: two runs started inside
    # the same second would share a directory and interleave their manifests.
    # Loaded before any agent module is imported, so the agents pick up the
    # guarded version when they bind their own reference.
    import structured_guard as _SG
    from run_toollog import pin

    _SG.install()
    import fin_context as _FC

    _FC.install()
    print("guard: native raw-reply preservation and current-state financial metrics verified")

    _t0 = __import__("datetime").datetime.now()
    stamp = "{}-{}".format(_t0.strftime("%Y%m%d-%H%M%S"), __import__("uuid").uuid4().hex[:8])
    if not args.no_probe:
        probe_lib.install()
        log = probe_lib.set_log(
            PROBE_DIR / (f"{args.log_tag}-{stamp}.jsonl"), {"effort": args.effort, "ticker": None}
        )
        print(f"guard: probe log {log.name}")

    # Per attempt, and kept apart. A first attempt that degraded and a second
    # that succeeded must not leave the first one's reasons on the second's
    # verdict.
    attempts = []
    attempt = 0
    verdict = None
    while True:
        attempt += 1
        print(f"\n=== attempt {attempt:d} ===")
        # Pin this run's log slice BEFORE the run appends to it, and take
        # the results dir from the config the run will actually use.
        from tradingagents.default_config import DEFAULT_CONFIG as _CFG

        run_log = pin(cli_args, _CFG["results_dir"])
        import exchange_sessions as _ES

        _ES.DEGRADED.clear()  # this attempt's own record only
        # One prefetch log per attempt, in this attempt's own directory.
        # Each recorder owns its file: a shared target is how a second
        # attempt appends into the first attempt's evidence.
        import prefetch_recorder as _PF

        _adir = REPO / "data" / "probe" / (f"{args.log_tag}-{stamp}")
        _adir.mkdir(parents=True, exist_ok=True)
        prefetch_log = _PF.install(_adir / (f"prefetch-{attempt:d}.log"))
        if prefetch_log is not None:
            print(f"guard: prefetch log {prefetch_log.path.name}")
        report_dir, cli_failed = _run_cli(cli_args)
        if run_log is not None:
            run_log.close()  # pin the end offset for later review

        if cli_failed:
            print("guard: the CLI failed; this run is not sound")
            verdict = {
                "ok": False,
                "results": [],
                "failed": [],
                "reason": "CLI exited non-zero",
                "data_gaps": [],
                "degraded": _degraded(),
                "starved_analysts": [],
                "analyst_data": {},
            }
        elif report_dir is None:
            # No report line: the run produced nothing. Falling back to
            # whatever is newest on disk is how a failed run gets graded
            # against the previous run''s leftovers, so refuse instead.
            print("guard: the run saved no report; refusing to grade an old one")
            verdict = {
                "ok": False,
                "results": [],
                "failed": [],
                "reason": "no report was produced by this run",
                "data_gaps": [],
                "degraded": _degraded(),
                "starved_analysts": [],
                "analyst_data": {},
            }
        else:
            report_dir = pathlib.Path(report_dir)
            if not args.no_probe:
                probe_lib.note_report(report_dir)
            print(f"guard: checking {report_dir.name}")
            fallback = [a.strip() for a in (args.analysts or "").split(",") if a.strip()]
            verdict = C.check(
                report_dir,
                fallback or None,
                [run_log, prefetch_log.path] if prefetch_log else run_log,
                _degraded(),
            )

        if not args.no_probe:
            _report_counts(probe_lib.classify())
        _write_evidence(
            _adir / (f"attempt-{attempt:d}.json"),
            {
                "report": str(report_dir) if report_dir else None,
                "tool_log": str(run_log.path) if run_log else None,
                "start": run_log.start if run_log else None,
                "end": run_log.end,
                "prefetch_log": str(prefetch_log.path) if prefetch_log else None,
                "verdict": ("ACCEPT" if verdict["ok"] else "REJECT") if verdict else None,
            },
        )
        attempts.append(
            {
                "attempt": attempt,
                "report": str(report_dir) if report_dir else None,
                "degraded": _degraded(),
            }
        )
        _print_verdict(verdict)

        if verdict["ok"]:
            print("guard: accepted")
            if len(attempts) > 1:
                _print_attempts(attempts)
            return 0
        if attempt > args.max_retries:
            print(f"guard: giving up after {attempt:d} attempt(s); this run is NOT sound")
            _print_attempts(attempts)
            return 1
        print(f"guard: retrying ({attempt:d}/{args.max_retries:d})")


def _degraded():
    """What the data layer gave up on during this run.

    A refresh that could not replace a mid-session cache raises, so the value
    never reaches a report; the reason still has to travel with the verdict
    rather than disappearing with the exception.
    """
    import exchange_sessions

    return list(exchange_sessions.DEGRADED)


def _run_cli(cli_args):
    """Run the CLI once. Returns (report_dir_or_None, failed_flag)."""
    import contextlib
    import io
    import re as _re

    from cli.main import app

    buf = io.StringIO()
    failed = False
    try:
        with contextlib.redirect_stdout(buf):
            app(cli_args)
    except SystemExit as exc:
        failed = bool(exc.code)
    except Exception as exc:
        print(f"guard: CLI raised {type(exc).__name__}: {exc}")
        failed = True
    printed = buf.getvalue()
    match = _re.search(r"Report saved to:\s*(\S+)", printed)
    return (match.group(1) if match else None), failed


def _report_counts(counts):
    print("-" * 72)
    print(
        "calls={:d}  structured_attempts={:d}  structured_ok={:d}  structured_parse_misses={:d}  original_text_preserved={:d}  empty_replies={:d}  tool_calls={:d}".format(
            len(counts["calls"]),
            counts["structured_attempts"],
            counts["structured_ok"],
            counts["structured_failed"],
            counts["structured_raw_retained"],
            len(counts["empty_replies"]),
            counts["tool_calls"],
        )
    )
    nodes = {}
    for rec in counts["calls"]:
        key = rec.get("node") or "<no-node>"
        nodes.setdefault(key, []).append(rec)
    if nodes:
        print("{!s:<26} {!s:>6} {!s:>8} {!s:>10}".format("node", "calls", "empty", "reasoning"))
        for name, rows in sorted(nodes.items()):
            tot = sum((r.get("usage") or {}).get("reasoning") or 0 for r in rows)
            print(
                "{!s:<26} {:>6d} {:>8d} {!s:>10}".format(
                    name,
                    len(rows),
                    sum(1 for r in rows if r.get("outcome") == "empty_reply"),
                    f"{tot:,}" if tot else "-",
                )
            )


def _write_evidence(path, payload):
    """One file per attempt, never overwritten.

    The offsets, the report path and the pre-fetch log together are what make a
    run reviewable after later runs have appended to the same framework log.
    """
    import json as _json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _print_attempts(attempts):
    """Every attempt and what it degraded on, shown whatever the outcome: a
    first attempt that failed is the reason a second one was needed."""
    print("-" * 72)
    print(f"ATTEMPTS ({len(attempts):d})")
    for a in attempts:
        print("   #{:d} report={!s}".format(a["attempt"], a["report"] or "<no report>"))
        deg = a["degraded"] or []
        if not deg:
            print("        no degradation recorded")
        for d in deg:
            print(
                "        DEGRADED {!s:<10} {!s:<8} {!s}".format(
                    d.get("symbol"), d.get("stage"), str(d.get("error"))[:54]
                )
            )


def _print_verdict(verdict):
    """Three lines, three different claims.

    STAGES is about text on disk. DATA is about what the data layer actually
    delivered. VERDICT is the acceptance decision. Keeping them apart is the
    point: a stage can be long, well written, and standing on nothing, and a
    run with no prices still prints a rating.
    """
    if verdict.get("reason"):
        print("VERDICT : REJECT ({})".format(verdict["reason"]))
        for d in verdict.get("degraded") or []:
            print(
                "          degraded {!s:<10} {!s:<8} {!s}".format(
                    d.get("symbol"), d.get("stage"), str(d.get("error"))[:56]
                )
            )
        return
    total = len(verdict["results"])
    failed = verdict.get("failed") or []
    rollup = verdict.get("analyst_data") or {}
    unproven = verdict.get("starved_analysts") or []
    ledger = verdict.get("data_ledger") or {"entries": []}

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
    print("VERDICT : %s" % ("ACCEPT" if verdict["ok"] else "REJECT"))
    if unproven:
        print(
            "          {} had no usable input; this is NOT a complete analysis".format(
                ", ".join(unproven)
            )
        )

    entries = ledger.get("entries") or []
    for e in sorted((x for x in entries if x["refused"]), key=lambda x: x["key"]):
        print("   refused  {!s:<26} {!s:<11} x{:d}".format(e["key"], e["reason"], e["refused"]))
    for e in sorted((x for x in entries if x["unknown"]), key=lambda x: x["key"]):
        print("   unknown  {!s:<26} {!s:<11} x{:d}".format(e["key"], e["reason"], e["unknown"]))
    for d in verdict.get("degraded") or []:
        print(
            "   degraded {!s:<22} {!s:<8} {!s}".format(
                d.get("symbol"), d.get("stage"), str(d.get("error"))[:44]
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
