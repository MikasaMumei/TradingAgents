r"""Process-level check: the runner has a working script entry point.

The suite called main() in-process, so it could not see that running the file
directly did nothing at all: no --help output, and a bogus flag still exited 0.
Both are covered here by actually launching the script, so nothing is billed.

    .venv\Scripts\python.exe tools_probe\test_entrypoint_offline.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
RUNNER = HERE / "guarded_run.py"
PY = pathlib.Path(sys.executable)
FAILURES = []


def run(*args):
    proc = subprocess.run(
        [str(PY), str(RUNNER), *args], capture_output=True, text=True, timeout=120
    )
    return proc.returncode, proc.stdout + proc.stderr


def check(name, ok, detail=""):
    print(
        "  [{}] {}{}".format(
            "PASS" if ok else "FAIL", name, ("  <- " + detail)[:150] if not ok else ""
        )
    )
    if not ok:
        FAILURES.append(name)


def main():
    check("the runner file exists", RUNNER.is_file(), str(RUNNER))

    code, out = run("--help")
    check("--help exits 0", code == 0, f"exit={code:d}")
    check("--help prints usage", "usage:" in out and "--effort" in out, out[:120])

    code, out = run("--no-such-flag")
    check("an unknown flag exits 2", code == 2, f"exit={code:d} out={out[:100]!s}")
    check("an unknown flag is reported", "unrecognized arguments" in out, out[:120])

    src = RUNNER.read_text(encoding="utf-8")
    check(
        "the script has a main entry point",
        'if __name__ == "__main__":' in src and "raise SystemExit(main())" in src,
    )

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES):d}):")
        for n in FAILURES:
            print("  -", n)
        return 1
    print("entry-point checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
