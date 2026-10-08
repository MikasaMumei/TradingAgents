r"""Each run is charged only for the gaps it actually hit.

The framework appends to <results>/<ticker>/<date>/message_tool.log, so a
re-run on the same pair reads the first one's lines too. Reading the whole file
would credit a run with gaps it never hit. The offset pinned before the run
starts is what keeps the two apart.

    .venv\Scripts\python.exe tools_probe\test_run_log_isolation.py
"""

import pathlib
import sys
import tempfile
from pathlib import Path

import check_run_complete as C
import run_toollog as R

sys.path.insert(0, str(Path(__file__).resolve().parent))

FAIL = []


def check(n, ok, d=""):
    print("  [{}] {}{}".format("PASS" if ok else "FAIL", n, ("  <- " + str(d)) if not ok else ""))
    if not ok:
        FAIL.append(n)


root = pathlib.Path(tempfile.mkdtemp())
(args) = ["--ticker", "AAPL", "--date", "2026-10-06", "--analysts", "market"]

# ç¬¬ä¸€æ¬¡è¿è¡Œï¼šå†™äº† 2 æ¡æ•°æ®ç¼ºå£
p1 = R.pin(args, root)
p1.path.parent.mkdir(parents=True, exist_ok=True)
with open(p1.path, "a", encoding="utf-8") as fh:
    fh.write(
        "12:00:00 [Data] DATA_UNAVAILABLE: no configured vendor could serve get_news right now\n"
    )
    fh.write(
        "12:00:01 [Data] DATA_UNAVAILABLE: no configured vendor could serve get_news right now\n"
    )
check(
    "pin points at the ticker/date folder",
    p1.path.name == "message_tool.log" and p1.path.parent.name == "2026-10-06",
    p1.path,
)
check(
    "the first run sees its own two refusals",
    sum(g["refused"] for g in C.data_gaps(p1) if g["key"] == "get_news") == 2,
    C.data_gaps(p1),
)

# ç¬¬äºŒæ¬¡è¿è¡Œï¼šåŒä¸€ ticker/dateï¼Œä½† offset ä¹‹åŽåªå†™ 1 æ¡åˆ«çš„ç¼ºå£
p2 = R.pin(args, root)
with open(p2.path, "a", encoding="utf-8") as fh:
    fh.write(
        "13:00:00 [Data] DATA_UNAVAILABLE: no configured vendor could serve get_macro_indicators right now\n"
    )
methods1 = {g["key"]: g["refused"] for g in C.data_gaps(p1)}
methods2 = {g["key"]: g["refused"] for g in C.data_gaps(p2)}
check("the second run does NOT inherit the first run's gaps", "get_news" not in methods2, methods2)
check("the second run sees only its own gap", methods2.get("get_macro_indicators") == 1, methods2)
check("the first run still reports its own two after that", methods1.get("get_news") == 2, methods1)

# è·¯å¾„æ¥è‡ªå®žé™…ä¼ å…¥çš„ results_dirï¼Œè€Œä¸æ˜¯é»˜è®¤å€¼
other = pathlib.Path(tempfile.mkdtemp())
p3 = R.pin(args, other)
check(
    "the results dir used is the one the run is configured with",
    str(other) in str(p3.path),
    p3.path,
)
check("a run with no log yet starts at 0", p3.start == 0)

# æ²¡æœ‰ ticker/date æ—¶ä¸çŒœ
check("no ticker and date means no guess", R.pin(["--analysts", "market"], root) is None)

print()
print("== the slice is bounded at both ends ==")
p3 = R.pin(args, root)
with open(p3.path, "a", encoding="utf-8") as fh:
    fh.write(
        "14:00:00 [Data] DATA_UNAVAILABLE: no configured vendor could serve get_macro_indicators right now\n"
    )
end = p3.close()
with open(p3.path, "a", encoding="utf-8") as fh:
    fh.write(
        "15:00:00 [Data] DATA_UNAVAILABLE: no configured vendor could serve get_later_tool right now\n"
    )
check(
    "a closed slice ignores what a later run appended",
    "get_later_tool" not in {g["key"] for g in C.data_gaps(p3)},
    C.data_gaps(p3),
)
check("a closed slice is re-readable unchanged", end == p3.end)

print()
if FAIL:
    print(f"FAILED ({len(FAIL):d}):")
    for n in FAIL:
        print("  -", n)
    raise SystemExit(1)
print("all run-log isolation checks passed")
