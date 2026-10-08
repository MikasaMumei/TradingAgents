"""Compatibility import; financial metrics are maintained in the core package."""

from tradingagents.dataflows.financial_metrics import build_summary, parse

__all__ = ["build_summary", "parse"]
