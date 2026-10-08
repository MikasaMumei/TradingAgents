#!/usr/bin/env python
"""数据工具探测：不跑 LLM，只测数据层到底能拿到什么。

用途：行情/财报/新闻供应商选型时，先用这个把「哪个源、哪个市场、哪类数据」
的真实返回内容和日期记录下来，再决定要不要花钱/改配置。
不要用完整 LLM 分析来调试数据层——那会把「数据源不通」和
「prompt 有问题」混在一起，浪费 API 额度还难定位。

用法（容器内）:
    docker compose run --rm -T --entrypoint python tradingagents \\
        -m tools_probe.probe --symbols AAPL 600519.SS 000001.SZ

输出：每个 (symbol, 数据源) 一段，记录
  - 成功/失败
  - 失败时的异常类型和原始消息
  - 成功时的内容前若干字符 + 识别出的日期（验证数据新鲜度）
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

TICKERS = ["AAPL", "600519.SS", "000001.SZ"]
TODAY = date.today()
START = (TODAY - timedelta(days=30)).isoformat()
END = TODAY.isoformat()

DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def _sample(text: str, n: int = 220) -> str:
    """把返回内容压成一行摘要，保留原始可读性。"""
    s = re.sub(r"\s+", " ", str(text)).strip()
    return s[:n] + ("…" if len(s) > n else "")


def _dates(text: str) -> list[str]:
    """从返回内容里抽出出现的日期，用来判断数据新鲜度。"""
    return sorted(set(DATE_RE.findall(str(text))))[-4:]


def call(fn: Callable, **kwargs) -> tuple[bool, str]:
    """调用数据函数，把异常压成一行。返回 (ok, 详情)。

    只接受位置无关的关键字参数；签名不匹配时用 inspect 过滤，
    这样同一个脚本能同时喂不同 vendor 的同名工具。
    """
    try:
        sig = inspect.signature(fn)
        accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
        out = fn(**accepted)
        return True, _sample(out)
    except Exception as exc:  # noqa: BLE001 — 探测脚本要报告任何失败
        msg = str(exc).replace("\n", " ")[:200]
        return False, f"{type(exc).__name__}: {msg}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=TICKERS)
    args = ap.parse_args()

    from tradingagents.dataflows.vendors import sec_edgar  # type: ignore
    from tradingagents.dataflows.vendors.yahoo.fundamentals import (  # type: ignore
        get_balance_sheet as yf_balance,
        get_fundamentals as yf_fundamentals,
        get_income_statement as yf_income,
    )
    from tradingagents.dataflows.vendors.yahoo.market import (  # type: ignore
        get_stock_stats_indicators_window,
        get_YFin_data_online,
    )
    from tradingagents.dataflows.vendors.yahoo.news import (  # type: ignore
        get_news_yfinance,
    )
    from tradingagents.dataflows.vendors.yahoo.snapshot import (  # type: ignore
        build_verified_market_snapshot,
    )

    # (标签, 函数, 关键字参数)
    probes: list[tuple[str, Callable, dict[str, Any]]] = [
        (
            "yahoo/日线OHLCV      ",
            get_YFin_data_online,
            {"symbol": None, "start_date": START, "end_date": END},
        ),
        (
            "yahoo/snapshot直连   ",
            build_verified_market_snapshot,
            {"symbol": None, "as_of_date": END, "look_back_days": 10},
        ),
        (
            "yahoo/技术指标MACD   ",
            get_stock_stats_indicators_window,
            {"symbol": None, "indicator": "macd", "as_of_date": END, "look_back_days": 30},
        ),
        ("yahoo/基本面概览     ", yf_fundamentals, {"ticker": None, "as_of_date": END}),
        (
            "yahoo/利润表         ",
            yf_income,
            {"ticker": None, "freq": "quarterly", "as_of_date": END},
        ),
        (
            "yahoo/资产负债表     ",
            yf_balance,
            {"ticker": None, "freq": "quarterly", "as_of_date": END},
        ),
        (
            "yahoo/新闻           ",
            get_news_yfinance,
            {"ticker": None, "start_date": START, "end_date": END},
        ),
        (
            "sec_edgar/利润表     ",
            sec_edgar.get_income_statement,
            {"ticker": None, "freq": "quarterly", "as_of_date": END},
        ),
        (
            "sec_edgar/资产负债表 ",
            sec_edgar.get_balance_sheet,
            {"ticker": None, "freq": "quarterly", "as_of_date": END},
        ),
    ]

    results: dict[str, dict[str, Any]] = {}
    for sym in args.symbols:
        print(f"\n{'=' * 78}\n{sym}\n{'=' * 78}")
        results[sym] = {}
        for label, fn, tmpl in probes:
            # 把 None 占位换成真实 symbol：yahoo 侧参数名是 symbol，
            # 财报类是 ticker，两者都试一遍。
            kwargs = {k: (sym if v is None else v) for k, v in tmpl.items()}
            ok, detail = call(fn, **kwargs)
            if not ok and "ticker" in kwargs:
                ok2, detail2 = call(fn, **{**kwargs, "symbol": sym})
                if ok2:
                    ok, detail = ok2, detail2
            mark = "✅" if ok else "❌"
            # 成功时附带抽出的日期，用来判断数据新鲜度（只报最晚的几个）
            suffix = ""
            if ok:
                ds = _dates(detail)
                suffix = f"   [最晚日期 {ds[-1]}]" if ds else "   [未抽到日期]"
            print(f"  {mark} {label} {detail}{suffix}")
            results[sym][label.strip()] = {
                "ok": ok,
                "detail": detail,
                "dates": _dates(detail) if ok else [],
            }

    print(f"\n{'=' * 78}\nJSON 摘要\n{'=' * 78}")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
