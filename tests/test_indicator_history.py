"""Full-window indicators and actual vendor dispatch must honor market evidence."""

import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from tradingagents.dataflows.vendors.yahoo import market, snapshot


def prices(rows):
    closes = pd.Series(range(1, rows + 1), dtype=float)
    return pd.DataFrame({
        "Date": pd.bdate_range("2025-01-01", periods=rows),
        "Open": closes, "High": closes + 1, "Low": closes - 1,
        "Close": closes, "Volume": 1000,
    })


@pytest.mark.parametrize("rows", [81, 199, 200])
def test_sma_window_readiness_matches_across_bulk_single_and_snapshot(monkeypatch, rows):
    data = prices(rows)
    monkeypatch.setattr(market, "load_ohlcv", lambda *a, **k: data.copy())
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda *a, **k: data.copy())
    day = data.iloc[-1]["Date"].strftime("%Y-%m-%d")
    bulk = market._get_stock_stats_bulk("SPCX", "close_200_sma", day)[day]
    single = market.get_stock_stats("SPCX", "close_200_sma", day)
    report = snapshot.build_verified_market_snapshot("SPCX", day)
    row = next(line for line in report.splitlines() if line.startswith("| close_200_sma |"))
    if rows < 200:
        assert "insufficient history" in bulk
        assert "insufficient history" in single
        assert "insufficient history" in row
    else:
        assert float(bulk) == pytest.approx(100.5)
        assert single == pytest.approx(100.5)
        assert "100.50" in row


def test_early_and_missing_close_windows_remain_unavailable(monkeypatch):
    data = prices(250)
    monkeypatch.setattr(market, "load_ohlcv", lambda *a, **k: data.copy())
    day = data.iloc[-1]["Date"].strftime("%Y-%m-%d")
    bulk = market._get_stock_stats_bulk("SPCX", "close_200_sma", day)
    assert "insufficient history" in bulk[data.iloc[198]["Date"].strftime("%Y-%m-%d")]
    assert float(bulk[data.iloc[199]["Date"].strftime("%Y-%m-%d")]) == pytest.approx(100.5)
    data.loc[249, "Close"] = float("nan")
    assert "insufficient history" in market.get_stock_stats("SPCX", "close_200_sma", day)


def test_guard_replaces_preimported_router_and_settlement_references():
    root = Path(__file__).parents[1]
    code = r'''
import sys,io
sys.path.insert(0,"tools_probe")
import pandas as pd
import exchange_sessions as E
from tradingagents.dataflows import router
from tradingagents.dataflows.vendors.yahoo import market
from tradingagents.memory import settlement
data=pd.DataFrame({"Open":[10.,11.],"High":[12.,13.],"Low":[9.,10.],
    "Close":[11.,12.],"Volume":[100,200]},index=pd.to_datetime(["2026-10-07","2026-10-08"]))
data.index.name="Date"
class Ticker:
    def __init__(self,*a):pass
    def history(self,**kw):return data.copy()
market.yf.Ticker=Ticker
router.get_vendor=lambda *a,**k:"yfinance"
E.last_settled_date=lambda *a,**k:pd.Timestamp("2026-10-07")
assert router.VENDOR_METHODS["get_stock_data"]["yfinance"] is market.get_YFin_data_online
assert settlement.get_closes is market.get_closes
assert E.install()
text=router.route_to_vendor("get_stock_data","SPCX","2026-10-07","2026-10-08")
body="\n".join(line for line in text.splitlines() if not line.startswith("#"))
rows=pd.read_csv(io.StringIO(body))
assert rows["Date"].tolist()==["2026-10-07"],rows["Date"].tolist()
closes=settlement.get_closes("SPCX","2026-10-07","2026-10-09")
assert list(closes.index.strftime("%Y-%m-%d"))==["2026-10-07"]
assert not E.install()
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=root, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("guarded", [False, True])
def test_parallel_indicator_loads_serialize_one_cache_transaction(tmp_path, guarded):
    root = Path(__file__).parents[1]
    code = r'''
import os,sys,threading,time
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
os.environ['TRADINGAGENTS_CACHE_DIR']=sys.argv[1]
sys.path.insert(0,'tools_probe')
from tradingagents.dataflows.vendors.yahoo import ohlcv
from tradingagents.dataflows.config import get_config
get_config()['data_cache_dir']=sys.argv[1]
active=maximum=0
count_lock=threading.Lock()
barrier=threading.Barrier(6)
data=pd.DataFrame({'Open':[10.,11.],'High':[12.,13.],'Low':[9.,10.],
    'Close':[11.,12.],'Volume':[100,200]},index=pd.to_datetime(['2026-10-07','2026-10-08']))
data.index.name='Date'
class Ticker:
    def __init__(self,*a):pass
    def history(self,**kw):
        global active,maximum
        with count_lock:
            active+=1;maximum=max(maximum,active)
        try:
            time.sleep(.03)
            return data.copy()
        finally:
            with count_lock:active-=1
ohlcv.yf.Ticker=Ticker
ohlcv._cache_is_fresh=lambda *a:False
if sys.argv[2]=='1':
    import exchange_sessions as E
    E.last_settled_date=lambda *a,**k:pd.Timestamp('2026-10-07')
    assert E.install()
def load(_):
    barrier.wait(timeout=10)
    return ohlcv.load_ohlcv('SPCX','2026-10-09',fill_gaps=False)
with ThreadPoolExecutor(max_workers=6) as pool:frames=list(pool.map(load,range(6)))
assert maximum==1,maximum
assert all(len(frame)>0 for frame in frames)
if sys.argv[2]=='1':assert all(frame['Date'].max()==pd.Timestamp('2026-10-07') for frame in frames)
'''
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(tmp_path), str(int(guarded))],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
