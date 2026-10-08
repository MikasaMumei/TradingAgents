"""可插拔日线取数层 —— TradingAgents 对本部署的最小上游补丁。

## 为什么存在

upstream 把所有日线取数收敛到 ``yf.Ticker(sym).history(...)`` 一处，
但 yfinance 走的是 Yahoo Finance。本机（腾讯云 ECS，出口 43.153.220.174）
访问 Yahoo 稳定返回 HTTP 429，行情/指标/snapshot/结算四条路径全部不可用。

这里提供**同形状**的替代取数（返回 DatetimeIndex + Open/High/Low/Close/Volume），
让 upstream 既有的缓存清洗、防未来函数过滤、stockstats 指标计算、
snapshot 渲染、收益结算**一行都不用改**。

## 为什么不是新 vendor 插件

upstream 的 vendor 体系（router.VENDOR_METHODS）只覆盖 router 认识的方法；
``build_verified_market_snapshot`` / ``get_company_profile`` / ``get_closes``
这三处是**直接 import yahoo**、绕过 router 的。新增 vendor 解决不了它们。
在取数层换源，一个接缝同时覆盖四条路径，且天然保证行情/指标/snapshot 同源同口径。

## 数据源选择

环境变量 ``TRADINGAGENTS_PRICE_SOURCE``：

============  ====================================================
``auto``      按市场自动路由（默认）：A 股 → 腾讯，美股/其他 → Twelve Data
``tencent``   全部走腾讯（仅 A 股有意义）
``twelvedata``  全部走 Twelve Data
``yfinance``  全部走 yfinance（= 恢复 upstream 原行为，用于对照）
============  ====================================================

``auto`` 模式下任一源失败都会回退到 yfinance，因此**不会**因为
免费源缺 key 就让整条链路不可用（回退失败会照常抛 VendorUnavailableError）。

## 口径纪律

- 腾讯返回**前复权(qfq)**（含分红送转），Twelve Data 日线官方默认
  ``adjust=splits``（**按拆股调整，不含分红**）—— 两者口径不同，不可混用同一根 bar。
  取数层在 ``FeedResult.adjustment`` 里如实标注来源与口径，混用检查交给调用方。
- 成交量单位：腾讯为「手」(1手=100股)，Twelve Data 为「股」。
  本模块统一换算成**股**，避免下游单位混淆。
- 分析日之后的数据由 upstream 既有逻辑过滤，本模块不处理。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

TENCENT_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
TWELVE_URL = "https://api.twelvedata.com/time_series"

# 腾讯 qfqday 的列序是 [日期, 开, 收, 高, 低, 成交量(手), {}]，
# 注意「收」在「高」之前，与常规 OHLC 顺序不同，解析时不能想当然。

# 单次请求超时；免费源偶发慢，重试交给调用方的 yf_retry 语义
_TIMEOUT = 30


@dataclass
class FeedResult:
    """取数结果 + 口径说明。frame 为空表示该源不可用。"""

    frame: pd.DataFrame
    source: str
    vendor_symbol: str
    adjustment: str
    volume_unit: str
    currency: str
    error: str = ""
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 工具


def _get(url: str, timeout: int = _TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "TradingAgents/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def classify_market(symbol: str) -> str:
    """返回 'CN' / 'US'。按 A 股代码形态判断，不查网络。"""
    s = (symbol or "").strip().upper().split(".")[0]
    if s.isdigit() and len(s) == 6:
        return "CN"
    if (symbol or "").strip().upper().endswith((".SS", ".SZ", ".SH", ".BJ")):
        return "CN"
    return "US"


def cn_to_tencent(symbol: str) -> str:
    """A 股代码 -> 腾讯代码。600519 / 600519.SS -> sh600519；000001.SZ -> sz000001。

    **显式交易所后缀优先于代码前缀**。这一点很关键：000001 既是平安银行（深市 sz），
    又是上证指数（沪市 sh）。靠首位猜会把 ``000001.SS``（上证指数）错认成
    ``sz000001``（平安银行），而 default_config 的 benchmark_map 把
    ``.SS`` 映射到上证指数做收益基准 —— 标的错了，收益比较就全错。
    """
    s = (symbol or "").strip().upper()
    suffix = s.split(".")[1] if "." in s else ""
    base = s.split(".")[0]
    if base[:2] in ("SH", "SZ", "BJ") and base[2:].isdigit():
        return base
    if not base.isdigit() or len(base) != 6:
        raise ValueError(f"不是合法的 A 股代码: {symbol}")

    # 1) 显式后缀优先：用户/上游已经指明交易所时，不再用代码前缀覆盖它
    if suffix in ("SS", "SH"):
        return f"sh{base}"
    if suffix == "SZ":
        return f"sz{base}"
    if suffix == "BJ":
        return f"bj{base}"

    # 2) 没有后缀才按代码前缀推断（6/5/9 沪，0/1/2/3 深，4/8 北）
    if base[0] in "69":
        return f"sh{base}"
    if base[0] in "0123":
        return f"sz{base}"
    if base[0] in "48":
        return f"bj{base}"
    raise ValueError(f"无法判定 A 股交易所: {symbol}")


def _empty(symbol: str, source: str, err: str) -> FeedResult:
    return FeedResult(pd.DataFrame(), source, symbol, "", "", "", err)


# ------------------------------------------------------------------ 腾讯行情


def volume_already_in_shares(vendor_symbol: str) -> bool:
    """腾讯接口的成交量单位**因品种而异**，返回 True 表示已经是「股」。

    实测（2026-10-01，本机直连腾讯）：

    =============  ======  ===============  ==========================
    品种           key     原始量            按「手」推算的成交额
    =============  ======  ===============  ==========================
    sh600519 沪主板 qfqday      38,331        48.2 亿  ← 合理 ⇒ 手
    sz000001 深主板 qfqday   1,045,357        12.1 亿  ← 合理 ⇒ 手
    sh688001 科创板 qfqday   7,597,613       461.1 亿  ← 荒谬 ⇒ 实为股
    sh000001 上证指数 day 414,560,247   1,592,819 亿  ← 荒谬 ⇒ 实为股
    =============  ======  ===============  ==========================

    结论：**科创板(sh688) 与指数(sh000/sz399) 已是「股」，其余个股是「手」**。
    统一 ×100 会让科创板和指数的成交量放大 100 倍。

    注意：AKShare 的 `stock_hist_tx.py:113` 排除表里含 `sz000`，
    那会把**平安银行(sz000001)** 也排除掉 —— 但实测它确实是「手」。
    所以这里**不照抄 AKShare**，只用经实测的两条规则。
    """
    vs = vendor_symbol.lower()
    return vs.startswith(("sh688", "sh000", "sz399"))  # 科创板与沪深指数


def fetch_tencent(symbol: str, start: str, end: str) -> FeedResult:
    """A 股日线。成交量统一换算成「股」；复权口径按实际返回的 key 标注。"""
    try:
        vsym = cn_to_tencent(symbol)
    except ValueError as exc:
        return _empty(symbol, "tencent", str(exc))

    # 腾讯的 count 参数是条数上限，起始日要往前留足余量
    want = 1300
    begin = (date.fromisoformat(start) - timedelta(days=want * 2 + 60)).isoformat()
    url = f"{TENCENT_URL}?_var=kline_dayqfq&param={vsym},day,{begin},{end},{want},qfq&r=0.5"
    try:
        raw = _get(url)
    except Exception as exc:  # noqa: BLE001 — 统一转成可回退的结果
        return _empty(symbol, "tencent", f"{type(exc).__name__}: {exc}")

    body = raw.split("=", 1)[1] if raw.lstrip().startswith("kline_dayqfq") else raw
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        return _empty(symbol, "tencent", f"响应非 JSON: {exc}")
    if doc.get("code") != 0:
        return _empty(symbol, "tencent", f"业务 code={doc.get('code')} {doc.get('msg')}")

    node = (doc.get("data") or {}).get(vsym) or {}
    # **口径必须按实际返回的 key 标注**。指数（sh000001 等）根本不返回 qfqday，
    # 只有 day —— 那是【不复权】价，标成「前复权」是错的。
    if node.get("qfqday"):
        rows, adjustment = node["qfqday"], "qfq(前复权)"
    elif node.get("day"):
        rows, adjustment = node["day"], "none(不复权，接口未提供前复权序列)"
    else:
        return _empty(symbol, "tencent", f"{vsym} 无 K 线数据 (keys={list(node.keys())})")

    already_shares = volume_already_in_shares(vsym)
    vol_scale = 1.0 if already_shares else 100.0

    recs = []
    for r in rows:
        if len(r) < 6:
            continue
        try:
            recs.append(
                {
                    "Date": r[0],
                    "Open": float(r[1]),
                    "Close": float(r[2]),
                    "High": float(r[3]),
                    "Low": float(r[4]),
                    "Volume": float(r[5]) * vol_scale,
                }
            )
        except (ValueError, TypeError, IndexError):
            continue
    if not recs:
        return _empty(symbol, "tencent", "K 线行无法解析为数值")

    df = pd.DataFrame(recs)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    notes = [
        f"原始成交量单位={'股' if already_shares else '手'}，"
        f"已{'保持' if already_shares else '×100 换算'}为股"
    ]
    if not adjustment.startswith("qfq"):
        notes.append("该标的接口不提供前复权序列，本次为不复权价，与个股的前复权序列**不可混用**")
    return FeedResult(df, "tencent", vsym, adjustment, "股", "CNY", notes=notes)


# ------------------------------------------------------------------ Twelve Data


def fetch_twelvedata(symbol: str, start: str, end: str) -> FeedResult:
    """美股日线。官方默认 adjust=splits（按拆股调整，不含分红）。

    未配置 key 或超出权限时返回空结果，以便上层回退 yfinance。
    """
    key = os.environ.get("TWELVEDATA_API_KEY") or os.environ.get("TWELVE_DATA_API_KEY")
    if not key:
        return _empty(symbol, "twelvedata", "未配置 TWELVEDATA_API_KEY")

    base = re.sub(r"[.-]", "-", (symbol or "").strip().upper())
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    # 官方 outputsize 上限就是 5000，不是无限；此处按上限收敛。
    outputsize = max(300, min(5000, int(days * 0.75) + 30))
    url = (
        f"{TWELVE_URL}?symbol={base}&interval=1day"
        f"&outputsize={outputsize}&start_date={start}&end_date={end}&apikey={key}"
    )
    try:
        doc = json.loads(_get(url))
    except Exception as exc:  # noqa: BLE001
        return _empty(symbol, "twelvedata", f"{type(exc).__name__}: {exc}")

    if doc.get("status") == "error":
        return _empty(symbol, "twelvedata", f"{doc.get('code')}: {str(doc.get('message'))[:120]}")
    values = doc.get("values") or []
    if not values:
        return _empty(symbol, "twelvedata", "空序列")

    recs = []
    for v in values:
        try:
            recs.append(
                {
                    "Date": v["datetime"][:10],
                    "Open": float(v["open"]),
                    "High": float(v["high"]),
                    "Low": float(v["low"]),
                    "Close": float(v["close"]),
                    "Volume": float(v.get("volume") or 0),
                }
            )
        except (KeyError, ValueError, TypeError):
            continue
    if not recs:
        return _empty(symbol, "twelvedata", "所有行无法解析为数值")

    df = pd.DataFrame(recs)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()

    meta = doc.get("meta") or {}
    # 日线的官方默认 adjust=splits：按拆股调整，但**不含**分红调整。
    # 标成「不复权」是错的，会让读者误以为可以直接和不复权序列混用。
    return FeedResult(
        df, "twelvedata", base, "splits(按拆股调整，不含分红)", "股", meta.get("currency") or "USD"
    )


# ------------------------------------------------------------------ yfinance 回退


def fetch_yfinance(symbol: str, start: str, end: str) -> FeedResult:
    """原 upstream 行为，保留作回退与对照。

    复用 upstream 自己的 ``yf_retry``（指数退避），不重新实现重试策略，
    也不改动 yfinance 的调用参数 —— 这样回退路径的失败行为和打补丁前完全一致。
    """
    try:
        import yfinance as yf

        from tradingagents.dataflows.symbols import normalize_symbol
        from tradingagents.dataflows.vendors.yahoo.common import yf_retry

        canonical = normalize_symbol(symbol)
        # yf_retry 对 None 返回值不做处理，history 抛限速异常时才会重试
        data = yf_retry(lambda: yf.Ticker(canonical).history(start=start, end=end))
        if data is None or data.empty:
            return _empty(symbol, "yfinance", "空序列")
        if data.index.tz is not None:
            data.index = data.index.tz_localize(None)
        return FeedResult(
            data, "yfinance", canonical, "auto_adjust(前复权，含分红送转)", "股", "USD"
        )
    except Exception as exc:  # noqa: BLE001
        return _empty(symbol, "yfinance", f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ 分发


def select_source(symbol: str, override: str | None = None) -> str:
    """决定该标的用哪个源。override 来自 TRADINGAGENTS_PRICE_SOURCE。"""
    mode = (override or os.environ.get("TRADINGAGENTS_PRICE_SOURCE") or "auto").lower()
    if mode in ("tencent", "twelvedata", "yfinance"):
        return mode
    return "tencent" if classify_market(symbol) == "CN" else "twelvedata"


def _clip(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """裁剪到 [start, end)，与 ``yf.Ticker().history()`` 的区间语义一致。

    ``history(start, end)`` 是 **start 闭、end 开**。upstream 依赖这个语义：
    - ``load_ohlcv`` 传 ``end=明天``，所以当天那根会被包含进来
    - ``get_YFin_data_online`` 传 ``end=请求日+1``，所以请求日会被包含进来
    - ``get_closes`` 直接传 ``end_date``，语义就是「不含 end_date 当天」

    本层必须自己裁剪：腾讯接口按「起始日 + 条数」返回，起始日往往比请求的
    start_date 早得多（为满足 1300 根而回溯），不裁就会把区间外的历史数据
    混进历史分析和收益结算 —— 曾导致 9/28 的新决策被当成「9/2 已完成五日持有」。
    """
    if frame is None or frame.empty:
        return frame
    idx = frame.index
    if not isinstance(idx, pd.DatetimeIndex):
        frame = frame.copy()
        idx = pd.DatetimeIndex(pd.to_datetime(idx))
        frame.index = idx
    lo = pd.Timestamp(start).normalize()
    # end 视为开区间上界（与 yfinance 的 history(start, end) 语义一致）
    hi = pd.Timestamp(end).normalize()
    return frame[(idx >= lo) & (idx < hi)]


def yfinance_fallback_enabled() -> bool:
    """是否允许回退到 yfinance。**默认关闭。**

    本机 Yahoo 恒 429，回退的唯一效果是多等 3 次退避重试（约 14 秒）后
    仍然失败，纯属浪费。但保留它有两个价值：
      - Yahoo 恢复时能自动生效，不必改配置
      - 作为「upstream 原行为」的显式对照组

    所以默认关闭，需要时显式打开：
      TRADINGAGENTS_PRICE_FALLBACK=yfinance
    打开后若连续失败 TRADINGAGENTS_PRICE_FALLBACK_CYCLES 次（默认 3），
    自动熔断不再重试，判定 Yahoo 不可用，避免每次取数都白等。
    """
    return (os.environ.get("TRADINGAGENTS_PRICE_FALLBACK") or "").strip().lower() in (
        "yfinance",
        "1",
        "true",
        "yes",
        "on",
    )


def _fallback_circuit_open() -> bool:
    """熔断器：连续失败达到阈值后不再尝试 yfinance。"""
    limit = (os.environ.get("TRADINGAGENTS_PRICE_FALLBACK_CYCLES") or "3").strip()
    try:
        limit = max(0, int(limit))
    except ValueError:
        limit = 3
    if limit <= 0:
        return False
    marker = os.path.join(tempfile.gettempdir(), "tradingagents-yfinance-circuit")
    try:
        with open(marker, encoding="utf-8") as fh:
            return int(fh.read().strip() or 0) >= limit
    except (OSError, ValueError):
        return False


def _record_fallback_failure() -> None:
    """记一次 yfinance 失败；成功则清零。"""
    marker = os.path.join(tempfile.gettempdir(), "tradingagents-yfinance-circuit")
    try:
        with open(marker, encoding="utf-8") as fh:
            n = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        n = 0
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(str(n + 1))
    except OSError:
        pass


def _clear_fallback_failures() -> None:
    marker = os.path.join(tempfile.gettempdir(), "tradingagents-yfinance-circuit")
    with contextlib.suppress(OSError):
        os.remove(marker)


def daily_ohlcv(symbol: str, start: str, end: str, override: str | None = None) -> FeedResult:
    """按配置取日线，返回序列一律裁剪到 ``[start, end)``（yfinance 语义）。

    回退到 yfinance **默认关闭**（见 :func:`yfinance_fallback_enabled`）。
    无论开没开，``FeedResult.source`` 都会如实记录最终用了谁，
    ``error`` 带上各源失败原因 —— 不掩盖，也不假装。
    """
    # 历史分析的口径警告：腾讯的**前复权**序列会随新的除权事件**回溯调整**。
    # 实测 600519 在 2025-09-30：不复权收盘 1443.99，前复权 1392.01。
    # 所以"今天下载的前复权序列里 2025-09-30 那根"**不是当日的成交价**。
    # upstream 的 as_of_date 过滤只挡"未来"数据，不解决复权回溯。
    historical = end < date.today().isoformat()
    if historical:
        _HIST_NOTE = (
            f"历史分析（end={end} < 今天）：本序列为**下载当天**口径。"
            "腾讯前复权会随新的除权送转事件回溯调整，"
            "因此序列中的历史价格不等于当日真实成交价"
            "（实测 600519 @2025-09-30：不复权 1443.99 / 前复权 1392.01）。"
            "做历史复盘前须按当时可得的复权信息重建序列。"
        )
    else:
        _HIST_NOTE = ""

    primary = select_source(symbol, override)
    order = [primary]
    if primary != "yfinance" and yfinance_fallback_enabled():
        if _fallback_circuit_open():
            order.append("__circuit_open__")
        else:
            order.append("yfinance")

    fetchers = {
        "tencent": fetch_tencent,
        "twelvedata": fetch_twelvedata,
        "yfinance": fetch_yfinance,
    }
    errors: list[str] = []
    for name in order:
        res = fetchers[name](symbol, start, end)
        if res.frame is not None and not res.frame.empty:
            res.frame = _clip(res.frame, start, end)
            if not res.frame.empty:
                if errors:
                    res.error = "；".join(errors)
                return res
            errors.append(f"{name}: 裁剪到 [{start}, {end}) 后为空")
            continue
        errors.append(f"{name}: {res.error}")
    res = _empty(symbol, primary, "；".join(errors) or "全部数据源均不可用")
    return res


# ------------------------------------------------------------------ 自检

if __name__ == "__main__":
    import sys

    sym = sys.argv[1] if len(sys.argv) > 1 else "600519"
    today = date.today()
    res = daily_ohlcv(sym, (today - timedelta(days=400)).isoformat(), today.isoformat())
    print(
        f"symbol={sym} source={res.source} vendor={res.vendor_symbol} "
        f"adj={res.adjustment} vol={res.volume_unit} ccy={res.currency}"
    )
    idx = res.frame.index
    first = str(idx[0])[:10] if len(idx) else "-"
    last = str(idx[-1])[:10] if len(idx) else "-"
    print(f"bars={len(res.frame)} {first} → {last}")
    if res.error:
        print(f"note: {res.error}")
    if len(res.frame):
        print(res.frame.tail(3).to_string())
