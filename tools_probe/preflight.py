#!/usr/bin/env python
"""运行前数据闸门：关键数据缺失时**不进入决策阶段**。

## 为什么要有

分析师拿不到数据时**不会抛异常**，工具只是返回一段
``DATA_UNAVAILABLE:`` / ``NO_DATA_AVAILABLE:`` 说明文字（见
``dataflows/router.py:176-197``）。分析师会读到这段文字，然后照常往下走 ——
research 辩论、trader、risk、portfolio 全都用**模型先验知识**补齐数字，
最终产出一份看起来很专业、实则没有数据支撑的决策。

已实证的污染：行情全失败时，模型产出了含「Services 增速 24%→13-15%」
「PE 30+」这类具体数字的完整 Underweight 决策，全部来自模型记忆。

## 本脚本做什么

对**本次启用的分析师**，逐个真实调用它会用的工具，检测返回文本里的
无数据标记，给出「能不能进决策阶段」的结论：

- 全部关键数据就绪 -> exit 0，可以跑
- 有分析师的关键数据缺失 -> exit 1，打印原因，**由调用方中止本次分析**

不调用任何 LLM，只打数据接口。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

# router 产出的无数据标记（见 dataflows/router.py:176-197）
NO_DATA_MARKERS = ("DATA_UNAVAILABLE", "NO_DATA_AVAILABLE")

# **不能只认上面两个标记。** upstream 还有一类"拿不到数据但不报错"的返回：
# 工具照常返回一段说明文字，只是内容是"这份数据被withheld / 取不到"。这类
# 文字里没有 DATA_UNAVAILABLE 标记，早前的闸门因此把它们当成"数据就绪"放行。
#
# 实测漏判：600519 [market,fundamentals] 返回下面两段，闸门却报 ✓ 放行：
#   "# Company Fundamentals for 600519 ... Profile fundamentals are withheld
#    for this date. This vendor serves only present-day values ..."
#   "# Balance Sheet for 600519 ... Balance Sheet data is withheld for this
#    date. ... SEC EDGAR serves US filers as filed."
# 基本面分析师实际上一条财务数据都没拿到，却过了闸门去写决策 —— 与闸门
# 要防的"模型用先验补数字"是同一类事故。
#
# 真实行情返回的是 CSV（数字+日期），下面这些英文词组不会出现在其中，
# 所以按词组定向匹配不会误伤。改这里之前先用 inspect 之类确认目标工具
# 在**有数据**时的返回不含这些词。
NO_DATA_PATTERNS = (
    # 词形要全：实测 fundamentals 分析师返回的是 "Profile fundamentals ARE
    # withheld"（复数），只匹配 "is withheld" 会漏掉它 —— 而这正是当初
    # 漏判的那一条。
    re.compile(r"\b(?:is|are|was|were) withheld\b", re.IGNORECASE),
    re.compile(r"\bdata (?:is|are) (?:not |un)?available\b", re.IGNORECASE),
    re.compile(r"\bnot a US SEC filer\b", re.IGNORECASE),
    re.compile(r"\bno (?:matching |relevant )?(?:news|articles?|data|results?)\b", re.IGNORECASE),
    re.compile(r"\b(?:unable|failed) to (?:fetch|retrieve|obtain|load)\b", re.IGNORECASE),
    re.compile(r"\b(?:fetch|retrieve|download) (?:failed|error)\b", re.IGNORECASE),
)


def _no_data_hit(text: str) -> str | None:
    """返回命中的无数据标记名；正常返回 None。"""
    if any(m in text for m in NO_DATA_MARKERS):
        return "标记"
    for pat in NO_DATA_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


# analyst key -> 该分析师的关键数据探针。
#
# key 必须用 upstream ``cli/models.py`` 的 AnalystType 实际取值
# (market / social / news / fundamentals)。早前这里写成 "sentiment"，
# 而 CLI 传进来的是 "social"（界面标签才是 Sentiment Analyst），
# 于是默认的四分析师组合里 social 匹配不到探针、被当作"无探针跳过"而放行。
#
# 判定规则：**所有列出的探针必须全部通过**。早前是"部分可用也放行"，
# 同样漏放（get_stock_data 挂了但 get_indicators 还能用时）。
CRITICAL_PROBES: dict[str, list[tuple[str, dict[str, str]]]] = {
    # (工具名, 额外关键字参数)
    "market": [("get_stock_data", {}), ("get_indicators", {"indicator": "macd"})],
    "news": [("get_news", {})],
    "fundamentals": [("get_fundamentals", {}), ("get_balance_sheet", {})],
    # social 分析师用 get_news + Reddit/StockTwits（仅美股）+ 可选 Jev。
    # 前两个源 A 股本就没有，所以只把 get_news 列为必需。
    "social": [("get_news", {})],
}


def _plain(fn: Any) -> Callable:
    """取 LangChain @tool 的底层函数，否则原样返回。"""
    return getattr(fn, "func", fn)


def _call(fn: Any, symbol: str, curr_date: str, **extra: Any) -> str:
    """调用工具并强制传入 symbol。

    工具声明里 symbol/ticker 是 InjectedState，LangChain 正常调用会从 state 注入。
    这里绕过注入层显式传参，才能在图之外单独探测。
    """
    import inspect

    plain = _plain(fn)
    sig = inspect.signature(plain)
    kwargs: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name in ("symbol", "ticker"):
            kwargs[name] = symbol
        elif name in ("curr_date", "as_of_date", "trade_date", "end_date"):
            kwargs[name] = curr_date
        elif name in extra:
            kwargs[name] = extra[name]
        elif param.default is inspect.Parameter.empty:
            # 必填但我们不关心的参数：给个不会崩的默认值
            kwargs[name] = extra.get(name, "")
    return str(plain(**kwargs))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument(
        "--analysts", default="market", help="逗号分隔，与 run.sh 的 --analysts 保持一致"
    )
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    curr = args.date
    start = (date.fromisoformat(curr) - timedelta(days=30)).isoformat()

    from tradingagents.agents import tools as T

    enabled = [a.strip() for a in args.analysts.split(",") if a.strip()]
    print(f"数据闸门  标的={args.ticker}  分析日={curr}  分析师={enabled}")

    blocked: list[str] = []
    unknown: list[str] = []
    for key in enabled:
        probes = CRITICAL_PROBES.get(key)
        if not probes:
            # 未知分析师名 = 没被检查过 = 不能放行。宁可误拦，不可误放。
            print(f"  ✗  {key:<12} **没有为该分析师定义数据探针，无法确认数据可用**")
            print(
                f"       合法名称: {', '.join(sorted(CRITICAL_PROBES))}"
                f"（取自 upstream cli/models.py 的 AnalystType）"
            )
            unknown.append(key)
            continue
        ok_tools, bad_tools = [], []
        for tool_name, extra in probes:
            fn = getattr(T, tool_name, None)
            if fn is None:
                bad_tools.append(f"{tool_name}(工具不存在)")
                continue
            try:
                out = _call(fn, args.ticker, curr, start_date=start, **extra)
            except Exception as exc:  # noqa: BLE001
                bad_tools.append(f"{tool_name}({type(exc).__name__}: {str(exc)[:70]})")
                continue
            hit = _no_data_hit(out)
            if hit is not None:
                head = out.strip().splitlines()[0][:88]
                bad_tools.append(f"{tool_name} → {head}")
            else:
                ok_tools.append(tool_name)

        if bad_tools:
            # 任一必需探针失败即拦截：分析师在缺一路数据时仍会产出报告，
            # 缺失部分由模型先验补齐，比直接失败更危险。
            print(
                f"  ✗  {key:<12} 有 {len(bad_tools)}/{len(probes)} 项关键数据不可用，"
                f"**不得进入决策阶段**"
            )
            for b in bad_tools:
                print(f"       - {b}")
            blocked.append(key)
        else:
            print(f"  ✓  {key:<12} 数据就绪（{', '.join(ok_tools)}）")

    if unknown:
        print()
        print(f"闸门结论：**停止**。以下分析师名称未被识别，未做数据检查：{', '.join(unknown)}")
        return 1

    if blocked:
        print()
        print(f"闸门结论：**停止**。以下分析师存在关键数据缺失：{', '.join(blocked)}")
        print("继续跑会让模型用先验知识编造数字，产出的决策不可用。")
        print("处理方式：填齐数据源 key（见 .env 注释），或用 --analysts 只跑有数据的分析师。")
        return 1

    print()
    print("闸门结论：**放行**。本次启用的分析师关键数据齐备。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
