r"""Offline checks for the guardrails. Nothing here calls a model.

Covers the four acceptance cases the wrapper must get right, and the two probe
cases that were wrong: a second consecutive call on one binding used to lose
that binding's tools, and two analysts running together used to trade them.

    .venv\Scripts\python.exe tools_probe\test_guardrails_offline.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


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

FAILURES = []


def check(name, ok, detail=""):
    print(
        "  [{}] {}{}".format("PASS" if ok else "FAIL", name, ("  <- " + detail) if not ok else "")
    )
    if not ok:
        FAILURES.append(name)


STAGE_FILES = [
    "2_research/manager.md",
    "2_research/bull.md",
    "2_research/bear.md",
    "3_trading/trader.md",
    "4_risk/aggressive.md",
    "4_risk/conservative.md",
    "4_risk/neutral.md",
    "5_portfolio/decision.md",
]
BODY = "# stage\n" + ("real analysis body. " * 40) + "\n"


def make_report(root, extra=(), analysts_header=None):
    for rel in list(STAGE_FILES) + list(extra):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(BODY, encoding="utf-8")
    if analysts_header:
        (root / "complete_report.md").write_text(
            f"# report\n\n- Rating: Hold\n- Analysts: {analysts_header}; research debate rounds 2\n",
            encoding="utf-8",
        )
    return root


# ------------------------------------------------------------------ 1


def test_acceptance():
    print("\n== acceptance checker ==")
    import check_run_complete as C

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="accept-"))

    v = C.check(make_report(tmp / "good", ["1_analysts/market.md"]), ["market"], _log(EVIDENCE))
    check("complete report is accepted", v["ok"], str(v.get("failed")))
    check(
        "check() returns a dict with ok/results/failed",
        all(k in v for k in ("ok", "results", "failed")),
    )

    stub = make_report(tmp / "stub", ["1_analysts/market.md"])
    (stub / "4_risk/aggressive.md").write_text("Aggressive Analyst: " * 3, encoding="utf-8")
    v = C.check(stub, ["market"])
    check("role-header-only stage is rejected", v["ok"] is False)
    check(
        "rejection names the cause",
        any(f.get("reason") == "role-headers-only" for f in v["failed"]),
        str([f.get("reason") for f in v["failed"]]),
    )

    v = C.check(tmp / "nope", ["market"])
    check("missing report directory is rejected", v["ok"] is False)

    v = C.check(make_report(tmp / "odd"), ["quant_wizard"])
    check("unknown analyst is rejected, not skipped", v["ok"] is False)
    check(
        "unknown analyst is named in the reason",
        any("no file mapping" in (f.get("reason") or "") for f in v["failed"]),
    )

    v = C.check(
        make_report(
            tmp / "hdr",
            ["1_analysts/market.md", "1_analysts/news.md"],
            analysts_header="market, news",
        ),
        None,
    )
    check(
        "analysts are read from the report itself",
        v.get("analysts") == ["market", "news"],
        str(v.get("analysts")),
    )


# ------------------------------------------------------------------ 2


class _Msg:
    def __init__(self, body="", calls=()):
        self.content = body
        self.tool_calls = list(calls)
        self.response_metadata = {
            "token_usage": {
                "prompt_tokens": 7,
                "completion_tokens": 11,
                "total_tokens": 18,
                "completion_tokens_details": {"reasoning_tokens": 3},
            }
        }


class _Gen:
    def __init__(self, body="", calls=(), finish="stop"):
        self.message = _Msg(body, calls)
        self.generation_info = {"finish_reason": finish}


class _Result:
    def __init__(self, gen):
        self.generations = [[gen]]
        self.llm_output = None


def test_probe():
    print("\n== probe ==")
    import probe_lib

    def rm(meta):
        return type(
            "RM", (), {"metadata": meta, "tags": [], "parent_run_id": None, "run_id": "x"}
        )()

    class FakeClient:
        model_name = "fake-model"
        max_tokens = 4096
        queue = []
        meta = {}
        n_calls = 2

        def _get_request_payload(self, messages):
            return {
                "messages": messages,
                "max_completion_tokens": 4096,
                "model": "fake-model",
                "reasoning_effort": "low",
                "stream": False,
                "temperature": 0.0,
            }

        def _generate(self, *a, **kw):
            return self.queue.pop(0)

        def bind_tools(self, tools, **kw):
            # The unpatched behaviour: a binding whose invoke goes through
            # _generate. install() wraps this to set the context var.
            client = self

            class B:
                def invoke(self, *a, **k):
                    for _ in range(client.n_calls):
                        client.queue = [_Result(_Gen("reply"))]
                        client._generate(
                            client, messages=[("human", "q")], run_manager=rm(client.meta)
                        )
                    return "done"

            return B()

        def with_structured_output(self, schema, **kw):
            client = self
            out = object()

            class S:
                def invoke(self, *a, **k):
                    client.queue = [_Result(_Gen(""))]
                    client._generate(client, messages=[("human", "s")], run_manager=rm(client.meta))
                    return out  # a parsed instance: the structured attempt worked

            return S()

    log = probe_lib.set_log(pathlib.Path(tempfile.mkdtemp()) / "probe.jsonl")
    probe_lib.install(FakeClient)

    def bind_and_call(names):
        FakeClient.bind_tools(FakeClient(), [{"function": {"name": n}} for n in names]).invoke("q")

    # --- a second consecutive call on one binding must keep that binding's tools
    FakeClient.meta = {"langgraph_node": "analyst_market", "langgraph_step": 3}
    bind_and_call(["get_price", "get_indicators"])
    recs = [r for r in probe_lib.records(log) if r.get("seq")]
    check(
        "both calls on one binding record both tools",
        len(recs) == 2 and all(r["req_tool_count"] == 2 for r in recs),
        str([(r["seq"], r["req_tool_count"]) for r in recs]),
    )
    check(
        "neither is recorded as empty",
        all(r.get("outcome") != "empty_reply" for r in recs),
        str([r.get("outcome") for r in recs]),
    )
    check(
        "node comes from langgraph metadata, not the prompt",
        all(r.get("node") == "analyst_market" for r in recs),
    )
    check(
        "call index increments per node",
        [r.get("call_index_for_node") for r in recs] == [1, 2],
        str([r.get("call_index_for_node") for r in recs]),
    )
    check(
        "request params recorded from the built payload",
        recs[0].get("reasoning_effort") == "low" and recs[0].get("max_tokens") == 4096,
    )

    # --- two analysts at once must not trade tools
    log2 = probe_lib.set_log(pathlib.Path(tempfile.mkdtemp()) / "probe2.jsonl")

    def worker(node, names):
        FakeClient.meta = {"langgraph_node": node, "langgraph_step": 1}
        FakeClient.n_calls = 1
        bind_and_call(names)
        FakeClient.n_calls = 2

    ts = [
        threading.Thread(target=worker, args=("analyst_market", ["get_price"])),
        threading.Thread(target=worker, args=("analyst_news", ["get_news", "get_macro"])),
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    by_node = {}
    for r in probe_lib.records(log2):
        if r.get("seq"):
            by_node.setdefault(r["node"], set()).add(tuple(r.get("req_tools") or []))
    check(
        "each analyst kept its own tools",
        by_node.get("analyst_market") == {("get_price",)}
        and by_node.get("analyst_news") == {("get_news", "get_macro")},
        str(by_node),
    )

    # --- the three outcomes are told apart
    log3 = probe_lib.set_log(pathlib.Path(tempfile.mkdtemp()) / "probe3.jsonl")
    FakeClient.meta = {"langgraph_node": "n1", "langgraph_step": 1}
    FakeClient.n_calls = 1
    bind_and_call([])  # free text, with a body
    FakeClient.queue = [_Result(_Gen(""))]
    FakeClient._generate(FakeClient(), messages=[("human", "x")], run_manager=rm(FakeClient.meta))
    FakeClient.queue = [_Result(_Gen("calling", [{"name": "x"}]))]
    FakeClient._generate(FakeClient(), messages=[("human", "x")], run_manager=rm(FakeClient.meta))
    counts = probe_lib.classify(log3)
    kinds = sorted({r.get("outcome") for r in counts["calls"]})
    check(
        "free_text / empty_reply / tool_call are distinct outcomes",
        kinds == ["empty_reply", "free_text", "tool_call"],
        str(kinds),
    )
    check("exactly one empty reply counted", len(counts["empty_replies"]) == 1)

    # --- a structured attempt is recorded as such, with its outcome
    log4 = probe_lib.set_log(pathlib.Path(tempfile.mkdtemp()) / "probe4.jsonl")
    FakeClient.with_structured_output(FakeClient(), dict).invoke("q")
    counts = probe_lib.classify(log4)
    check(
        "structured attempt recorded and marked successful",
        counts["structured_attempts"] == 1 and counts["structured_ok"] == 1,
        str((counts["structured_attempts"], counts["structured_ok"])),
    )
    check(
        "structured call is labelled, not counted as free text",
        all(
            r.get("structured_schema")
            for r in counts["calls"]
            if r.get("outcome") == "structured_attempt"
        ),
        str([(r.get("structured_schema"), r.get("outcome")) for r in counts["calls"]]),
    )

    check("log holds no key material", "OPENAI_COMPATIBLE" not in log.read_text(encoding="utf-8"))


def main():
    test_acceptance()
    test_probe()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES):d}):")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all offline checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
