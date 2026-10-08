"""Run the real-parser regressions without network or model billing."""

from pathlib import Path

import pytest

if __name__ == "__main__":
    raise SystemExit(
        pytest.main(
            [str(Path(__file__).resolve().parents[1] / "tests/test_structured_reply.py"), "-q"]
        )
    )
