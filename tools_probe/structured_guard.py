"""Compatibility entry for guarded_run; raw preservation now lives in core."""

from tradingagents.agents import structured


def install():
    """Verify the native fix without rebinding imported agent functions."""
    if not hasattr(structured, "invoke_structured_reply"):
        raise RuntimeError("The native raw-output patch must be applied before guarded_run")
    return structured.invoke_structured_or_freetext
