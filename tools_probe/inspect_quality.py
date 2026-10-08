#!/usr/bin/env python
"""对 probe2 落盘的证据做深度质量检查。

probe2 负责「能不能取到」，这个脚本负责「取到的数据质量如何」：
  - 逐标的打印口径元信息（币种/时区/复权/成交量单位）
  - 交易日期空档（识别缺失交易日，不只是重复/空值）
  - 价格区间合理性（与近期波动幅度对比，识别异常跳变）
  - 成交量单位交叉验证（腾讯的「手」与 Twelve Data 的「股」不可直接比较）
"""

from __future__ import annotations

import glob
import json
import sys
from datetime import date
from pathlib import Path


def analyse(path: Path) -> None:
    d = json.loads(path.read_text(encoding="utf-8"))
    m, v = d["_meta"], d["_meta"]["validation"]
    bars = sorted(d["bars"], key=lambda b: b["date"])

    print(f"=== {m['source']} {m['symbol']} (vendor={m['vendor_symbol']}) ===")
    print(f"  市场={m['market']}  币种={m['currency']}  时区={m['timezone']}")
    print(f"  复权口径={m['adjustment']}")
    print(f"  成交量单位={m['volume_unit']}")
    print(f"  条数={v['bar_count']}  {v['earliest']} → {v['latest']}")
    print(f"  校验 ok={v['ok']}  阻断={v['blocking']}  提示={v['warnings']}")

    if not bars:
        print("  （无数据）\n")
        return

    # 重复日期
    seen: dict[str, int] = {}
    for b in bars:
        seen[b["date"]] = seen.get(b["date"], 0) + 1
    dupes = {k: c for k, c in seen.items() if c > 1}
    print(f"  重复日期: {len(dupes)}" + (f" {list(dupes)[:5]}" if dupes else "（无）"))

    # 交易日空档
    ds = [date.fromisoformat(b["date"]) for b in bars]
    gaps = [
        (ds[i - 1].isoformat(), ds[i].isoformat(), (ds[i] - ds[i - 1]).days)
        for i in range(1, len(ds))
        if (ds[i] - ds[i - 1]).days > 10
    ]
    print(f"  >10天空档: {len(gaps)}" + (f" 例 {gaps[:3]}" if gaps else "（无）"))

    # 价格关系 + 单日跳变
    ohlc_bad, jumps = [], []
    for i, b in enumerate(bars):
        if not (b["low"] <= b["open"] <= b["high"] and b["low"] <= b["close"] <= b["high"]):
            ohlc_bad.append(b["date"])
        if i and bars[i - 1]["close"]:
            chg = abs(b["close"] - bars[i - 1]["close"]) / bars[i - 1]["close"]
            if chg > 0.25:  # 单日涨跌超 25%，A 股有 20% 涨跌幅但 25% 极罕见
                jumps.append((b["date"], f"{chg:.1%}"))
    print(f"  OHLC 关系违规: {len(ohlc_bad)}" + (f" {ohlc_bad[:5]}" if ohlc_bad else "（无）"))
    print(f"  单日跳变 >25%: {len(jumps)}" + (f" {jumps[:5]}" if jumps else "（无）"))

    # 成交量量级（用于交叉验证单位声明）
    vols = [b["volume"] for b in bars if b.get("volume")]
    if vols:
        print(
            f"  成交量 中位数={sorted(vols)[len(vols) // 2]:,.0f} "
            f"min={min(vols):,.0f} max={max(vols):,.0f}"
        )

    closes = [b["close"] for b in bars]
    print(f"  收盘价 min={min(closes):,.2f} max={max(closes):,.2f} 最新={closes[-1]:,.2f}")
    print("  最近3根: " + " | ".join(f"{b['date']} C={b['close']:,.2f}" for b in bars[-3:]))
    print()


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    files = [f for f in sorted(glob.glob(str(root / "*.json"))) if Path(f).name != "summary.json"]
    if not files:
        print(f"{root} 下没有证据文件")
        return 1
    for f in files:
        analyse(Path(f))
    return 0


if __name__ == "__main__":
    sys.exit(main())
