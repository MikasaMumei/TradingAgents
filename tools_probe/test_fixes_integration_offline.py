"""Run isolated native patch regressions; never write production logs."""

from pathlib import Path

import pytest

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "tests"
    raise SystemExit(
        pytest.main(
            [str(root / "test_structured_reply.py"), str(root / "test_financial_metrics.py"), "-q"]
        )
    )
