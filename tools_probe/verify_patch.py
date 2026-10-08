#!/usr/bin/env python
"""补丁后的上游函数功能验证：四条路径是否真的同源同口径。

handoff 第二步要求「行情、技术指标和 verified snapshot 使用同一份缓存日线」。
upstream 的单测把 yfinance mock 掉了，正好绕过我改的取数层，所以这里做真实调用：

    get_YFin_data_online        行情 CSV
    get_stock_stats_indicators_window  技术指标
    build_verified_market_snapshot     核验 snapshot（原先直连 Yahoo）
    get_closes                  结算用收盘价

对同一标的比对四者的日期集合与价格，确认一致。
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta

AS_OF = date.today().isoformat()
START = (date.today() - timedelta(days=120)).isoformat()

DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")

# upstream 的工具在拿不到数据时**不抛异常**，而是返回一段说明文字
# （router 把 VendorUnavailableError 转成 "no data" 字符串）。
# 只 try/except 会把这种软失败判成成功 —— 之前就这么误报过。
SOFT_FAIL_MARKERS = (
    "DATA_UNAVAILABLE",
    "NO_DATA",
    "NO DATA",
    "unavailable",
    "no data",
    "rate limited",
    "Vendor '",
    "不可用",
    "无数据",
)


def soft_failed(text: str) -> str:
    """返回命中的失败标记；空串表示看起来是真数据。"""
    if not isinstance(text, str) or not text.strip():
        return "空返回"
    for marker in SOFT_FAIL_MARKERS:
        if marker in text:
            return marker
    return ""


def last_dates(text: str, n: int = 3) -> list[str]:
    return sorted(set(DATE_RE.findall(str(text))))[-n:]


def closes_from_csv(text: str) -> dict[str, float]:
    """从 upstream 返回的行情 CSV 里取 日期->收盘。"""
    out: dict[str, float] = {}
    lines = [ln for ln in str(text).splitlines() if ln and not ln.startswith("#")]
    # 表头形如 Date,Open,High,Low,Close,Volume
    hdr = lines[0].split(",") if lines else []
    try:
        di, ci = hdr.index("Date"), hdr.index("Close")
    except ValueError:
        return out
    for ln in lines[1:]:
        cells = ln.split(",")
        if len(cells) <= max(di, ci):
            continue
        try:
            out[cells[di]] = float(cells[ci])
        except ValueError:
            continue
    return out


def main() -> int:
    symbols = sys.argv[1:] or ["600519", "000001.SZ", "AAPL"]
    from tradingagents.dataflows.price_feed import daily_ohlcv, select_source

    from tradingagents.dataflows.vendors.yahoo.market import (
        get_closes,
        get_stock_stats_indicators_window,
        get_YFin_data_online,
    )
    from tradingagents.dataflows.vendors.yahoo.snapshot import (
        build_verified_market_snapshot,
    )

    # 快照价与行情价的容差。**必须为 0** —— 四条路径走同一个取数层，
    # 数值理应逐位相同；出现任何差异都是口径混用的信号。早前允许 0.5%
    # 会把这个信号吞掉。
    PRICE_TOL = 0.0

    rc = 0
    for sym in symbols:
        routed = select_source(sym)
        print(f"\n{'=' * 72}\n{sym}   (路由到: {routed})\n{'=' * 72}")
        prices: dict[str, float] = {}
        # 记录本次取数**实际**用了哪个源、什么口径，四条路径逐一对账
        feed = daily_ohlcv(sym, START, AS_OF)
        if feed.frame is not None and not feed.frame.empty:
            print(
                f"  取数层实际: source={feed.source}  复权={feed.adjustment}  "
                f"成交量={feed.volume_unit}  币种={feed.currency}"
            )
            for n in feed.notes:
                print(f"    · {n}")
            if feed.error:
                print(f"    · 备注: {feed.error[:110]}")
            if not feed.adjustment.startswith("qfq"):
                print("    · ⚠️ 非前复权序列，与个股前复权序列不可混用")
        else:
            print(f"  取数层实际: 无数据 ({feed.error[:100]})")

        # 1) 行情
        try:
            csv = get_YFin_data_online(sym, START, AS_OF)
            prices = closes_from_csv(csv)
            print(f"  ✅ 行情 get_YFin_data_online  {len(prices)} 行  最新 {last_dates(csv, 1)}")
            if prices:
                d = sorted(prices)[-1]
                print(f"       最新收盘 {d} = {prices[d]:,.2f}")
            else:
                print("       ⚠️ 未解析出价格行")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 行情: {type(exc).__name__}: {str(exc)[:160]}")
            rc = 1

        # 2) 技术指标
        try:
            ind = get_stock_stats_indicators_window(sym, "macd", AS_OF, 30)
            bad = soft_failed(ind)
            if bad:
                print(f"  ❌ 指标: 软失败（返回文本含 {bad!r}）")
                rc = 1
            else:
                print(f"  ✅ 指标 MACD  最新日期 {last_dates(ind, 1)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 指标: {type(exc).__name__}: {str(exc)[:160]}")
            rc = 1

        # 3) 核验 snapshot（原先直连 Yahoo，不走 router）
        try:
            snap = build_verified_market_snapshot(sym, AS_OF, 10)
            bad = soft_failed(snap)
            if bad:
                print(f"  ❌ snapshot: 软失败（返回文本含 {bad!r}）")
                rc = 1
                snap = ""
            else:
                got = last_dates(snap, 1)
                print(f"  ✅ snapshot  最新日期 {got}")
            # snapshot 里的最新收盘必须与行情一致
            m = re.search(r"close[^\d]*([\d,]+\.\d+)", snap or "", re.I)
            if prices and m:
                snap_close = float(m.group(1).replace(",", ""))
                last_px = prices[sorted(prices)[-1]]
                # 容差为 0：四条路径走同一个取数层，数值理应逐位相同。
                # 早前允许 0.5%，会把「口径混用」这个信号吞掉。
                same = abs(snap_close - last_px) <= PRICE_TOL * last_px
                print(
                    f"       snapshot 收盘 {snap_close:,.2f} vs 行情 {last_px:,.2f} → "
                    f"{'✅ 逐位一致' if same else '❌ 不一致（口径混用？）'}"
                )
                if not same:
                    rc = 1
            else:
                print("       ⚠️ 未能在 snapshot 中解析出收盘价，无法对账")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ snapshot: {type(exc).__name__}: {str(exc)[:160]}")
            rc = 1

        # 4) 结算收盘价
        try:
            closes = get_closes(sym, START, AS_OF)
            if len(closes) == 0:
                print("  ❌ get_closes: 返回空序列（结算拿不到价格）")
                rc = 1
            else:
                first, last = str(closes.index[0])[:10], str(closes.index[-1])[:10]
                print(f"  ✅ get_closes  {len(closes)} 个点  {first} → {last}")
                # 区间契约：start 闭、end 开。不含 end 当天，也不得早于 start。
                if first < START or last >= AS_OF:
                    print(f"       ❌ 越界: 应落在 [{START}, {AS_OF})")
                    rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ get_closes: {type(exc).__name__}: {str(exc)[:160]}")
            rc = 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
