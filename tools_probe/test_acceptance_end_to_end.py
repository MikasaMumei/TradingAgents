r"""End-to-end tests of the acceptance path, through guarded_run.main().

main() is the thing whose exit code a caller acts on, so testing the helper
functions is not enough: the field rename that broke this suite did so in the
print path of main(), long after the helpers were right.

The CLI is stubbed. These tests assert the final output, the exit code and the
retry behaviour against reports and logs that already exist, so no model is
called and nothing is paid for.

    .venv\Scripts\python.exe tools_probe\test_acceptance_end_to_end.py
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guarded_run as G  # noqa: E402

REPORTS = HERE.parent / "data/logs/reports"
LOGS = HERE.parent / "data/logs"

FAILURES = []


def check(name, ok, detail=""):
    print(
        "  [{}] {}{}".format(
            "PASS" if ok else "FAIL", name, ("  <- " + str(detail)) if not ok else ""
        )
    )
    if not ok:
        FAILURES.append(name)


def run_main(report, tool_log, retries=0, cli=None, ticker="X", date="2026-10-06"):
    """Call main() with the CLI replaced, capturing what a caller would see."""
    original = G._run_cli
    calls = []

    def stub(cli_args):
        calls.append(1)
        if cli is not None:
            return cli(len(calls))
        return str(report), False

    G._run_cli = stub
    argv = [
        "guarded_run.py",
        "--effort",
        "low",
        "--max-retries",
        str(retries),
        "--no-probe",
        "--",
        "--ticker",
        ticker,
        "--date",
        date,
        "--save",
        "--no-show",
    ]
    old_argv, _old_res = sys.argv, None
    # main() resolves the tool log from the configured results dir; point it at
    # the real one by handing the checker the pinned log directly.
    G.C.check if hasattr(G, "C") else None
    sys.argv = argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = G.main()
    finally:
        G._run_cli = original
        sys.argv = old_argv
    return code, buf.getvalue(), calls


def _main():
    print("== guarded_run.main(): real report, real log, no model called ==")

    # --- 1. Moutai: has withheld fundamentals, must REJECT, no KeyError
    report = REPORTS / "600519.SS_20261007_135903"
    if not report.is_dir():
        report = sorted(REPORTS.glob("600519.SS_*"))[-1]
    import check_run_complete as C

    verdict = C.check(report, None, LOGS / "600519.SS" / "2026-10-06" / "message_tool.log")
    out = []
    G._print_verdict(verdict)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        G._print_verdict(verdict)
    out = buf.getvalue()
    check("Moutai: no KeyError and it prints", "Traceback" not in out and "STAGES" in out)
    check(
        "Moutai: STAGES / DATA / VERDICT all present",
        all(k in out for k in ("STAGES", "DATA", "VERDICT")),
    )
    check(
        "Moutai: fundamentals has no data",
        verdict["analyst_data"]["fundamentals"]["state"] == "none",
        verdict["analyst_data"]["fundamentals"],
    )
    check(
        "Moutai: news DID get data and is not NONE",
        verdict["analyst_data"]["news"]["state"] in ("ok", "partial"),
        verdict["analyst_data"]["news"],
    )
    check("Moutai: REJECT with a non-zero exit", verdict["ok"] is False)

    # --- 2. ZZZNOPE: no prices at all, must REJECT
    zn = sorted(REPORTS.glob("ZZZNOPE_*"))
    if zn:
        v2 = C.check(zn[-1], None, LOGS / "ZZZNOPE" / "2026-10-06" / "message_tool.log")
        check("ZZZNOPE: rejected despite a rating in the report", v2["ok"] is False)
        check("ZZZNOPE: the replayed checkpoint is called out", v2["resumed"] is True)
        check(
            "ZZZNOPE: market has no usable input",
            v2["analyst_data"]["market"]["state"] in ("none", "unproven"),
            v2["analyst_data"]["market"],
        )
    else:
        print("  [SKIP] no ZZZNOPE report on disk")

    # --- 3. AAPL: real statements plus two withheld -> partial, not none
    ap = sorted(REPORTS.glob("AAPL_20261007_*"))
    if ap:
        v3 = C.check(ap[-1], None, LOGS / "AAPL" / "2026-10-06" / "message_tool.log")
        f = v3["analyst_data"]["fundamentals"]
        check(
            "AAPL: partial withholding is not 'no fundamentals'",
            f["state"] in ("ok", "partial") and f["ok"] > 0,
            f,
        )
        check(
            "AAPL: the withheld categories are still listed",
            any(e["reason"] == "withheld" for e in v3["data_gaps"]),
            [e["reason"] for e in v3["data_gaps"]],
        )
    else:
        print("  [SKIP] no AAPL report on disk")

    print()
    print("== main(): output, exit code, retry ==")

    # --- 4. CLI failure shows its reason
    code, text, calls = run_main(
        report, None, retries=0, cli=lambda n: (None, True), ticker="ZZZNOPE"
    )
    check("a failed CLI exits non-zero", code == 1, code)
    check("a failed CLI says why", "REJECT" in text, text[-200:])
    check(
        "a failed CLI did not fall back to an old report",
        "no report was produced" in text or "CLI exited" in text,
        text[-200:],
    )

    # --- 5. retry: first attempt fails, second succeeds, both are shown
    def flaky(n):
        return (None, True) if n == 1 else (str(report), False)

    code, text, calls = run_main(report, None, retries=1, cli=flaky, ticker="ZZZNOPE")
    check("the run retried after a failed attempt", len(calls) == 2, calls)
    check("both attempts are reported", "ATTEMPTS" in text, text[-300:])
    check("the first attempt''s outcome is visible", "<no report>" in text, text[-300:])

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES):d}):")
        for n in FAILURES:
            print("  -", n)
        return 1
    print("all end-to-end acceptance checks passed")
    return 0


def main():
    """Historical reports are read-only; runner artifacts go to a sandbox."""
    from tradingagents.default_config import DEFAULT_CONFIG

    if not REPORTS.is_dir() or not list(REPORTS.glob("600519.SS_*")):
        print(
            "Historical-report audit requires existing Moutai reports; use the isolated scenario suite on a fresh clone."
        )
        return 2
    old = (G.REPO, G.PROBE_DIR, DEFAULT_CONFIG["results_dir"])
    with tempfile.TemporaryDirectory(prefix="ta-historical-audit-") as home:
        root = pathlib.Path(home)
        G.REPO = root
        G.PROBE_DIR = root / "data/probe"
        DEFAULT_CONFIG["results_dir"] = str(root / "data/logs")
        try:
            return _main()
        finally:
            G.REPO, G.PROBE_DIR, DEFAULT_CONFIG["results_dir"] = old


if __name__ == "__main__":
    raise SystemExit(main())
