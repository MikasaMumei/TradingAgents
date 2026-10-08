"""Compatibility entry; the fundamentals node reads current-state metrics natively."""

from tradingagents.agents.analysts import fundamentals_analyst


def install():
    """Fail clearly on an upstream checkout without the native metrics patch."""
    if not hasattr(fundamentals_analyst, "summary_from_state"):
        raise RuntimeError("The native financial-metrics patch must be applied first")
    return fundamentals_analyst.summary_from_state
