#!/usr/bin/env python
"""免费行情源探测：验证「拿到的数据能不能用」，不是「请求有没有返回」。

依据 cc-handoff.md §4 第一步：
- 每个标的至少 250 根**已收盘**交易日日线
- 记录条数 / 最早 / 最新 / 时区 / 币种 / 复权口径 / 成交量单位
- 校验重复日期、空值、价格关系（low <= open/close <= high）、正价格
- 最新 bar 必须是已收盘交易日；当日未收盘的 bar 不作收盘分析依据
- 失败必须分类：network / permission / quota / empty / stale / format
- 完整响应落盘做证据；URL 与日志中的 key 一律脱敏

只做数据层验证，不调用任何 LLM。

用法:
    python probe.py --out /probe-out --symbols AAPL MSFT NVDA 600519 000001
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- 数据源定义

TENCENT = "tencent_qfq"
TWELVE = "twelvedata"

MARKET_TZ = {
    "US": "America/New_York",
    "CN": "Asia/Shanghai",
}

# 供应商给的免费演示凭据，仅用于连通性验证，不构成正式账号权限证明
DEMO_KEY = "demo"
TEN_CN_TZ = ZoneInfo("Asia/Shanghai")
TEN_US_TZ = ZoneInfo("America/New_York")

MIN_BARS = 250


# ---------------------------------------------------------------- 失败分类


class ProbeError(Exception):
    kind = "unknown"

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _fetch(url: str, timeout: int = 30) -> str:
    """取回文本，失败时按可区分的原因分类抛出。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TradingAgents-probe/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", errors="replace")[:200]
        if exc.code in (401, 403):
            raise ProbeError("permission", f"HTTP {exc.code} 权限不足: {body}") from exc
        if exc.code == 429:
            raise ProbeError("quota", f"HTTP 429 配额/限流: {body}") from exc
        raise ProbeError("network", f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ProbeError("network", f"网络不可达: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProbeError("network", "请求超时") from exc
    except Exception as exc:  # noqa: BLE001
        raise ProbeError("network", f"{type(exc).__name__}: {exc}") from exc


def _redact(url: str) -> str:
    """把 key 类查询参数换成 ****，证据落盘和日志都用这个版本。"""
    return re.sub(
        r"(apikey|token|key|access_token)=([^&]*)", lambda m: f"{m.group(1)}=****", url, flags=re.I
    )


# ---------------------------------------------------------------- 标准化结构


@dataclass
class Series:
    source: str
    market: str
    symbol: str
    vendor_symbol: str
    currency: str
    timezone: str
    adjustment: str
    volume_unit: str
    bars: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    request_url: str = ""


# ---------------------------------------------------------------- 腾讯行情


def tencent_symbol(raw: str) -> tuple[str, str]:
    """A 股代码 -> 腾讯代码。600519 -> sh600519；000001 -> sz000001。"""
    s = raw.strip().upper()
    # 已带交易所后缀的先剥掉，避免出现 sh600519.SH
    base = s.split(".")[0]
    if base.startswith(("SH", "SZ", "BJ")) and base[2:].isdigit():
        base = base[2:]
    if not base.isdigit():
        raise ProbeError("format", f"不是合法的 A 股数字代码: {raw}")
    if len(base) != 6:
        raise ProbeError("format", f"A 股代码应为 6 位数字: {raw}")
    # 6/5/9 开头是沪市，0/1/2/3 开头是深市，4/8 开头是北交所
    if base[0] in "69":
        return f"sh{base}", "SH"
    if base[0] in "0123":
        return f"sz{base}", "SZ"
    if base[0] in "48":
        return f"bj{base}", "BJ"
    raise ProbeError("format", f"无法判定交易所: {raw}")


def probe_tencent(raw_symbol: str, want: int) -> Series:
    vsym, _ = tencent_symbol(raw_symbol)
    start = (date.today() - timedelta(days=want * 2 + 30)).isoformat()
    end = date.today().isoformat()
    url = (
        f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        f"?_var=kline_dayqfq&param={vsym},day,{start},{end},{want},qfq&r=0.5"
    )
    raw = _fetch(url)

    # 响应带 `_var=` 前缀，形如 kline_dayqfq={...}，需要剥掉
    body = raw.split("=", 1)[1] if raw.lstrip().startswith("kline_dayqfq") else raw
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProbeError("format", f"响应不是合法 JSON: {exc}") from exc

    if doc.get("code") != 0:
        raise ProbeError("format", f"业务 code={doc.get('code')} msg={doc.get('msg')}")

    node = (doc.get("data") or {}).get(vsym) or {}
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        raise ProbeError("empty", f"{vsym} 返回空 K 线 (data keys={list(node.keys())})")

    # 列序: [日期, 开, 收, 高, 低, 成交量, {}, ...]
    bars = []
    for r in rows:
        if len(r) < 6:
            continue
        try:
            bars.append(
                {
                    "date": r[0],
                    "open": float(r[1]),
                    "close": float(r[2]),
                    "high": float(r[3]),
                    "low": float(r[4]),
                    "volume": float(r[5]),
                }
            )
        except (ValueError, TypeError):
            continue
    if not bars:
        raise ProbeError("format", "所有 K 线行都无法解析为数值")

    notes = ["复权口径=qfq(前复权)，与原始不复权价格不可混用"]
    return Series(
        source=TENCENT,
        market="CN",
        symbol=raw_symbol,
        vendor_symbol=vsym,
        currency="CNY",
        timezone="Asia/Shanghai",
        adjustment="qfq(前复权)",
        volume_unit="手(1手=100股)，非股",
        bars=bars,
        notes=notes,
        request_url=url,
    )


# ---------------------------------------------------------------- Twelve Data


def probe_twelve(raw_symbol: str, want: int, api_key: str) -> Series:
    url = (
        f"https://api.twelvedata.com/time_series?symbol={raw_symbol}"
        f"&interval=1day&outputsize={want}&apikey={api_key}"
    )
    raw = _fetch(url)
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProbeError("format", f"响应不是合法 JSON: {exc}") from exc

    if doc.get("status") == "error":
        code = str(doc.get("code", ""))
        msg = str(doc.get("message", ""))
        if "limit" in code or "limit" in msg.lower() or "credits" in msg.lower():
            raise ProbeError("quota", f"配额不足: {code} {msg}")
        if "invalid" in code.lower() or "apikey" in code.lower():
            raise ProbeError("permission", f"key 无效: {code} {msg}")
        raise ProbeError("format", f"供应商报错: {code} {msg}")

    values = doc.get("values") or []
    if not values:
        raise ProbeError("empty", f"{raw_symbol} 返回空序列: {json.dumps(doc)[:150]}")

    bars = []
    for v in values:
        try:
            bars.append(
                {
                    "date": v["datetime"][:10],
                    "open": float(v["open"]),
                    "high": float(v["high"]),
                    "low": float(v["low"]),
                    "close": float(v["close"]),
                    "volume": float(v.get("volume") or 0),
                }
            )
        except (KeyError, ValueError, TypeError):
            continue
    if not bars:
        raise ProbeError("format", "所有行都无法解析为数值")

    meta = doc.get("meta") or {}
    notes = [
        f"exchange={meta.get('exchange')} currency={meta.get('currency')}",
        f"timezone={meta.get('timezone')} type={meta.get('type')}",
        "复权口径：Twelve Data 基础接口不做复权，返回原始 OHLC",
    ]
    return Series(
        source=TWELVE,
        market="US",
        symbol=raw_symbol,
        vendor_symbol=raw_symbol,
        currency=meta.get("currency") or "USD",
        timezone=meta.get("timezone") or "America/New_York",
        adjustment="none(不复权)",
        volume_unit="股(shares)",
        bars=bars,
        notes=notes,
        request_url=url,
    )


# ---------------------------------------------------------------- 校验


def validate(series: Series, min_bars: int = MIN_BARS) -> dict:
    """返回校验报告；把「有问题」分成阻断级和提示级。"""
    bars = sorted(series.bars, key=lambda b: b["date"])
    dates = [b["date"] for b in bars]
    blocking: list[str] = []
    warnings: list[str] = []

    # 重复日期
    dupes = sorted({d for d in dates if dates.count(d) > 1})
    if dupes:
        blocking.append(f"重复交易日 {len(dupes)} 个，例如 {dupes[:5]}")

    # 排序与完整性
    if dates != sorted(dates):
        warnings.append("返回顺序非升序（已在报告中重排）")

    # 空值 / 非法值
    bad_rows = [
        b["date"]
        for b in bars
        if any(b[k] is None or b[k] != b[k] for k in ("open", "high", "low", "close"))
    ]
    if bad_rows:
        blocking.append(f"{len(bad_rows)} 行含 NaN/None 价位，例如 {bad_rows[:5]}")

    # 价格关系
    ohlc_bad = []
    nonpositive = []
    for b in bars:
        if None in (b["open"], b["high"], b["low"], b["close"]):
            continue
        if not (b["low"] <= b["open"] <= b["high"] and b["low"] <= b["close"] <= b["high"]):
            ohlc_bad.append(b["date"])
        if min(b["open"], b["high"], b["low"], b["close"]) <= 0:
            nonpositive.append(b["date"])
    if ohlc_bad:
        blocking.append(f"{len(ohlc_bad)} 行违反 low<=open/close<=high，例如 {ohlc_bad[:5]}")
    if nonpositive:
        blocking.append(f"{len(nonpositive)} 行价格非正，例如 {nonpositive[:5]}")

    # 条数
    if len(bars) < min_bars:
        blocking.append(f"仅 {len(bars)} 根，少于要求的 {min_bars} 根")

    # 新鲜度：最新 bar 必须是已收盘交易日。
    # 交接要求「未收盘的当日 bar 不作为收盘分析依据」——这属于**阻断**而不是提示：
    # 当日 bar 是盘中价，拿它当收盘价算出的指标全是错的。
    tz = ZoneInfo(series.timezone) if series.timezone in MARKET_TZ.values() else None
    last = bars[-1]["date"] if bars else None
    today_local = datetime.now(tz).date().isoformat() if tz else date.today().isoformat()
    if last == today_local and tz:
        now_local = datetime.now(tz)
        close_h = 16 if series.market == "US" else 15
        if now_local.hour < close_h:
            blocking.append(
                f"最新 bar 是当日({last})且尚未收盘"
                f"（{series.timezone} 现在 {now_local:%H:%M}，收盘 {close_h}:00）"
                f"—— 盘中价不可作为收盘分析依据"
            )
    # 过期：超过 7 天没有新数据，说明源没在更新，不能拿它做当日分析
    age_days = (date.today() - date.fromisoformat(last)).days if last else None
    if age_days is not None and age_days > 7:
        blocking.append(f"数据已过期：最新 {last}，距今 {age_days} 天（>7 天）")

    return {
        "ok": not blocking,
        "bar_count": len(bars),
        "earliest": dates[0] if dates else None,
        "latest": dates[-1] if dates else None,
        "blocking": blocking,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 主流程

SOURCES = {
    "tencent": ("CN", "600519 000001"),
    "twelve": ("US", "AAPL MSFT NVDA"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/probe-out")
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--api-key", default=DEMO_KEY, help="Twelve Data key，默认官方 demo")
    ap.add_argument("--want", type=int, default=300, help="请求的日线条数")
    ap.add_argument("--min-bars", type=int, default=MIN_BARS)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    targets: list[tuple[str, str]] = []
    for sym in args.symbols:
        if sym.isdigit() or sym.upper().startswith(("SH", "SZ", "BJ")):
            targets.append(("tencent", sym))
        else:
            targets.append(("twelve", sym))
    if not targets:
        targets = [
            ("tencent", "600519"),
            ("tencent", "000001"),
            ("twelve", "AAPL"),
            ("twelve", "MSFT"),
            ("twelve", "NVDA"),
        ]

    summary: list[dict] = []
    for source, sym in targets:
        row: dict[str, Any] = {"source": source, "symbol": sym}
        t0 = time.monotonic()
        try:
            series = (
                probe_tencent(sym, args.want)
                if source == "tencent"
                else probe_twelve(sym, args.want, args.api_key)
            )
            report = validate(series, args.min_bars)

            f = out / f"{source}-{sym}.json"
            f.write_text(
                json.dumps(
                    {
                        "_meta": {
                            "source": series.source,
                            "symbol": series.symbol,
                            "vendor_symbol": series.vendor_symbol,
                            "market": series.market,
                            "currency": series.currency,
                            "timezone": series.timezone,
                            "adjustment": series.adjustment,
                            "volume_unit": series.volume_unit,
                            "notes": series.notes,
                            "validation": report,
                            "request_url": _redact(series.request_url),
                            "fetched_at_utc": datetime.now(UTC).isoformat(),
                        },
                        "bars": series.bars,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )

            row.update(
                {
                    "result": "PASS" if report["ok"] else "FAIL",
                    "vendor_symbol": series.vendor_symbol,
                    "market": series.market,
                    "currency": series.currency,
                    "timezone": series.timezone,
                    "adjustment": series.adjustment,
                    "volume_unit": series.volume_unit,
                    **{
                        k: report[k]
                        for k in ("bar_count", "earliest", "latest", "blocking", "warnings")
                    },
                    "evidence": f.name,
                    "elapsed_s": round(time.monotonic() - t0, 2),
                }
            )
            print(
                f"{'✅' if report['ok'] else '⚠️ '} {source:<8} {sym:<8} "
                f"{report['bar_count']:>4} bars  {report['earliest']} → {report['latest']}"
            )
            for m in report["blocking"]:
                print(f"     阻断: {m}")
            for m in report["warnings"]:
                print(f"     提示: {m}")
        except ProbeError as exc:
            row.update(
                {
                    "result": f"FAIL:{exc.kind}",
                    "error": exc.message,
                    "elapsed_s": round(time.monotonic() - t0, 2),
                }
            )
            print(f"❌ {source:<8} {sym:<8} [{exc.kind}] {exc.message}")
        except Exception as exc:  # noqa: BLE001
            row.update(
                {
                    "result": "FAIL:unknown",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_s": round(time.monotonic() - t0, 2),
                }
            )
            print(f"❌ {source:<8} {sym:<8} [unknown] {type(exc).__name__}: {exc}")
        summary.append(row)
        time.sleep(1.0)  # 别把免费额度打爆

    passed = sum(1 for r in summary if r.get("result") == "PASS")
    print(f"\n{'=' * 70}\n通过 {passed}/{len(summary)}")
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"证据目录: {out}")
    return 0 if passed == len(summary) else 1


if __name__ == "__main__":
    sys.exit(main())
