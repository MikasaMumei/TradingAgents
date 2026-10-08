# 上游补丁

> 历史说明：下文记录云端 v0.5.2 / `8b22d43d` 的行情补丁，不是当前 Windows v0.6.0 的维护说明。Windows 的实际源码、原始回答保留和财务摘要修复以 [Windows 维护说明](../docs/windows-maintenance.md)及 `0002-windows-runtime-guards.patch` 为准。不要把 0001 直接应用到 Windows 0.6.0；本页旧路径、行数和“未提交”状态仅描述当时快照。

本目录记录对 `TradingAgents/`（upstream）的**全部**改动。
本文是补丁的**唯一技术真相源**——缺陷台账、数据闸门验证表、口径说明只在这里维护一份。

原则遵循 `readme.md` §19：尽量不改上游；必须改时保持最小、可审阅、可恢复。

---

## 基线

| 项 | 值 |
|---|---|
| upstream | `https://github.com/TauricResearch/TradingAgents.git` |
| commit | `8b22d43d01d9ddda5d686d093d5385884622f3de`（v0.5.2） |
| 补丁文件 | `0001-pluggable-price-feed.patch` |
| 影响面 | 3 个文件（2 改 + 1 新增），改动 +520/−22 |
| 补丁自包含 | ✅ `price_feed.py`（471 行）**包含在补丁里**，`git apply` 一次搞定，不需要单独拷 |

**⚠️ 补丁改的三个文件从未 commit**，只存在于 `TradingAgents/` 的工作区：

```
 M tradingagents/dataflows/vendors/yahoo/market.py
 M tradingagents/dataflows/vendors/yahoo/ohlcv.py
?? tradingagents/dataflows/price_feed.py
```

**本目录是唯一副本。** 任何 `git checkout -- .` / `git stash` / 重新 clone
都会永久丢失这三个文件。

`patches/price_feed.py` 是**冗余副本**，不是必需项——补丁里已经有一份。
两份的 md5 应始终相同（`2e1661cae606200f1d14431f00fe7600`），可用来交叉验证：

```bash
# 在打过补丁的工作区里核对
md5sum tradingagents/dataflows/price_feed.py ../patches/price_feed.py
```

---

## 补丁清单

### 0001 可插拔日线取数层

**问题**：本机（腾讯云 ECS，公网出口 `43.153.220.174`）访问 Yahoo Finance
稳定返回 HTTP 429，upstream 全部日线取数（行情 / 指标 / snapshot / 结算）不可用。

> 换机器后**必须先探测**再决定要不要这套：
> ```bash
> curl -s -o /dev/null -w '%{http_code}\n' -m 20 \
>   'https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=5d&interval=1d'
> ```
> `200` = 不需要补丁，`DEPLOY-NEW-HOST.md` 走分支 A。

**为什么不新增 vendor 插件**：upstream 的 `router.VENDOR_METHODS` 只覆盖 router
认识的方法，而以下三处是**直接 import yahoo、绕过 router** 的：

- `agents/tools.py` → `build_verified_market_snapshot`（核验 snapshot）
- `agents/context.py` → `get_company_profile`（公司身份）
- `memory/settlement.py` → `get_closes`（收益结算）

新增 vendor 解决不了它们。改取数层则一个接缝覆盖全部路径。

**改动**：

| 文件 | 改动 |
|---|---|
| `dataflows/price_feed.py` | **新增**。可插拔取数层：按市场路由到腾讯（A 股）或 Twelve Data（美股），失败可回退 yfinance。返回与 `yf.Ticker().history()` 完全同形状，下游清洗 / 防未来函数过滤 / stockstats 指标 / snapshot 渲染 / 结算**一行未改** |
| `dataflows/vendors/yahoo/ohlcv.py` | `load_ohlcv` 的下载分支改走取数层；缓存文件名加入数据源后缀 |
| `dataflows/vendors/yahoo/market.py` | `get_YFin_data_online`、`get_closes` 改走取数层 |

**三处接缝**（全仓库只有这 3 处调 `yf.Ticker().history()`）：

```
vendors/yahoo/ohlcv.py   load_ohlcv          → 指标 / snapshot / 缓存
vendors/yahoo/market.py  get_YFin_data_online → 行情
vendors/yahoo/market.py  get_closes           → 收益结算
```

### 关键设计决策

**① 缓存文件名带数据源**：腾讯返回**前复权**、Twelve Data 按拆股调整，
成交量单位也不同。文件名不带来源，换源后会静默复用旧口径的缓存，造成
一次运行内混用两套复权口径。原名 `{symbol}-YFin-data.csv`，
现为 `{symbol}-{实际来源}-data.csv`。

**② 缓存名用「实际来源」而非「计划来源」**：写入按 `FeedResult.source`。
否则回退取到的 Yahoo 数据会存成 `AAPL-twelvedata-data.csv`。

**③ 缓存只读主源**：更早一版会在主源缓存缺失时回退去读其它源的当日缓存 ——
按实际来源命名只解决了"文件名不写错"，没解决"读到另一口径"。
现在只认主源：配置说用哪个源就读哪个源的缓存，缺了就重新取。
代价是回退场景下缓存写而不读，换来"一次分析内不可能混口径"。

**④ yfinance 回退默认关闭 + 熔断**：本机 Yahoo 恒 429，回退唯一效果是多等 3 次退避
（约 14 秒）后仍失败。需要时显式打开：

```env
TRADINGAGENTS_PRICE_FALLBACK=yfinance      # 默认关闭
TRADINGAGENTS_PRICE_FALLBACK_CYCLES=3      # 连续失败 N 次后熔断，不再重试
```

**⑤ 区间统一裁剪到 `[start, end)`**：与 `yf.Ticker().history()` 的 **end 开区间**
语义一致。upstream 三个调用点的契约全部保持：

| 调用点 | 传入的 end | 语义 |
|---|---|---|
| `load_ohlcv` | 明天 | 当天被包含 |
| `get_YFin_data_online` | 请求日 +1 | 请求日被包含 |
| `get_closes` | `end_date` | 语义就是「不含 end 当天」 |

**⑥ 成交量统一换算成「股」**，但**按品种分流**——见下方「口径说明」。

**⑦ 未改 upstream 的指标计算**。stockstats 仍算指标，本补丁只换数据来源。

**⑧ 失败方向取保守**。任一数据源失败都返回空结果并如实记录
`FeedResult.source` / `FeedResult.error` —— 宁可报错，也不返回来路不明的数据。

---

## 口径说明

| 源 | 市场 | key | 复权 | 成交量单位 |
|---|---|---|---|---|
| 腾讯行情 | A股**个股** | **免** | **前复权**（含分红送转） | 手（×100 换算为股） |
| 腾讯行情 | A股**指数** | **免** | **不复权**（接口不提供 `qfqday`） | 已是股（不 ×100） |
| Twelve Data | 美股 | 需 key | **按拆股调整（`adjust=splits`），不含分红** | 股 |
| yfinance | 两者 | 免 | 前复权 | 股 |

**⚠️ 复权口径不兼容**：腾讯是前复权、Twelve Data 是按拆股调整，
**两者不可混用于同一根 bar**。指数的不复权序列也不可与个股前复权混用。

**⚠️ 前复权的时间敏感性**：腾讯前复权价格会随新的除权事件**回溯调整**。
实测 600519 @2025-09-30：不复权收盘 **1443.99**，前复权 **1392.01**。
所以"今天下载的前复权序列里 2025-09-30 那根"**不是当日的真实成交价**。
upstream 的 `as_of_date` 过滤只挡"未来"数据，**不解决复权回溯**。

取数层已在 `end < 今天` 时自动附加口径警告到 `FeedResult.notes`，
但**按当时可得的复权信息重建序列尚未实现** —— 历史复盘前仍需人工核对该日期。

**成交量单位因品种而异**（实测，非照抄第三方）：

| 品种 | 原始量 | 换算 | 依据 |
|---|---:|---|---|
| `sh600519` 沪主板 | 38,331 | **手** → ×100 | 按手算成交额 48.2 亿，合理 |
| `sz000001` 深主板 | 1,045,357 | **手** → ×100 | 12.1 亿，合理 |
| `sh688001` 科创板 | 7,597,613 | **已是股** → 不换算 | 按手算 461 亿，荒谬 |
| `sh000001` 上证指数 | 414,560,247 | **已是股** → 不换算 | 按手算 159 万亿，荒谬 |

**未照抄 AKShare**：`stock_hist_tx.py:113` 的排除表含 `sz000`，会误排
平安银行（实测是「手」）。本项目只按 `sh688` / `sh000` / `sz399` 三条规则。

### 交易所代码解析（`price_feed.cn_to_tencent`）

**显式后缀优先于代码前缀** —— 这是修过的 bug（台账 #2）：早前只按前缀推断，
`000001.SS`（沪市收益基准）被映射成 `sz000001` 平安银行，标的错了收益比较全错。

| 输入 | 走哪条 | 输出 | 说明 |
|---|---|---|---|
| `600519.SS` / `600519.SH` / `sh600519` | 显式后缀 / 已带前缀 | `sh600519` | |
| `000001.SZ` | 显式后缀 | `sz000001` | 平安银行 |
| `000001.SS` | 显式后缀 | `sh000001` | 上证指数，**与上一行同代码不同标的** |
| `600519`（无后缀） | 前缀推断 | `sh600519` | 首字 `69` → 沪 |
| `000001`（无后缀） | 前缀推断 | `sz000001` | 首字 `0123` → 深 |
| `430047`（无后缀） | 前缀推断 | `bj430047` | 首字 `48` → 北 |

不能解析则抛 `ValueError`，不猜。

### 对接第三方接口的三个坑

重接或换源时按这几条核对，都是实测踩出来的：

1. **腾讯响应带 `_var=` 赋值前缀**。原始 body 是 `kline_dayqfq={...}`，
   直接 `json.loads` 会失败，需先 `split("=", 1)[1]` 剥掉。
2. **腾讯列序是 `[日期, 开, 收, 高, 低, 成交量(手), {}]`** ——
   **「收」在「高」之前**。按 `[开, 高, 低, 收]` 的直觉去解会把收盘价当最高价，
   而且 `low <= open/close <= high` 这类校验**依然能通过**，很难在验收时发现。
3. **Twelve Data `outputsize` 官方上限 5000**。按自然日估算所需条数时要留余量
   （周末 + 假日约占 30%），本实现用 `max(300, min(5000, int(days*0.75)+30))`。

---

## 缺陷台账

两轮复核共发现并修复 **11 个**。前 4 个是补丁自身的，后 7 个是补丁**周边**
的（缓存、闸门、验收脚本、成交量标注）。

| # | 缺陷 | 影响 | 修复与验证 |
|---|---|---|---|
| 1 | **日期区间未过滤** | 腾讯按「起始日+条数」返回，起始日远早于请求的 `start_date`，区间外数据混进历史分析与结算；曾致 9/28 的新决策被算成「9/2 已完成五日持有」 | 取数层统一裁剪 `[start, end)`。实测 `get_closes("600519","2026-09-01","2026-09-08")` → 精确 5 点 |
| 2 | **上证指数被映射成平安银行** | `000001.SS` 走代码前缀规则变成 `sz000001`；显式后缀判断是**不可达的死代码**。`000001.SS` 是沪市收益基准，标的错了收益比较全错 | 显式交易所后缀**优先于**代码前缀。实测 `000001.SS`→`sh000001`（上证 3,823.62）、`000001.SZ`→`sz000001`（平安 11.30） |
| 3 | **回退缓存混淆来源** | Twelve Data 失败后取到的 Yahoo 数据会存成 `AAPL-twelvedata-data.csv`，导致「行情走主源、snapshot 读缓存」时不同源 | 缓存名按**实际来源**写入，且**只读主源**缓存。⚠️ 本机 Yahoo 恒 429，跨源场景只能靠代码路径审阅 + 构造测试验证 |
| 4 | **验收脚本误报通过** | `verify_patch.py` 只 try/except，工具返回 `DATA_UNAVAILABLE` 文字（不抛异常）时判成功；`probe2.py` 对未收盘/过期只警告仍判通过 | verify 加软失败标记检测 + 结算区间越界校验；probe2 把「未收盘当日 bar」「超 7 天过期」升级为**阻断** |
| 5 | **闸门误放行** | ① 探针表写 `sentiment`，而 CLI 传 upstream `AnalystType.SOCIAL = "social"` → 默认四分析师组合里 social 匹配不到探针，被当"无探针跳过"而**放行**。② 判定是"部分可用也放行"，`get_stock_data` 挂、`get_indicators` 还能用时同样放行 | 探针名对齐 `market/social/news/fundamentals`；**所有必需探针全通过**；未知名不再跳过而是拦截 |
| 6 | **跨来源缓存** | 主源缓存缺失时回读其它源的当日缓存 → 行情走主源、指标/snapshot 读另一源 | 只读主源缓存。实测"磁盘只有 `AAPL-yfinance-data.csv` + 主源 twelvedata" → 正确报 `NoMarketDataError`；主源切 yfinance 时该缓存即合法 |
| 7 | **腾讯成交量一律 ×100** | 科创板与指数的原始量**本身就是「股」**，统一 ×100 放大 100 倍（`sh688001` 7,597,613 → 759,761,300） | 按品种分流（`sh688`/`sh000`/`sz399` 已是股）。**未照抄 AKShare** |
| 8 | **`day` 被标成前复权** | 指数不返回 `qfqday`，只有 `day`（不复权），代码却统一标注「前复权」 | 按实际返回的 key 如实标注：`none(不复权，接口未提供前复权序列)` + "不可与个股前复权混用"提示 |
| 9 | **历史前复权 ≠ 当日成交价** | 前复权随除权回溯调整（600519 @2025-09-30：不复权 1443.99 / 前复权 1392.01），历史复盘用的不是当日真实价 | `end < 今天` 时自动附口径警告。**重建序列未实现**，见「口径说明」 |
| 10 | **验收容差过宽** | 允许 snapshot 收盘与行情差 0.5% 且不核对来源 → 「四路径一致」的证据被高估 | 容差收紧为 **0**，并逐路径报出实际 `source`/`复权`/`成交量单位`/`币种` |
| 11 | **闸门只认 2 个标记，漏判「withheld」类无数据** | 闸门只搜 `DATA_UNAVAILABLE` / `NO_DATA_AVAILABLE`。upstream 还有一类**照常返回一段说明文字、内容却是「这份数据取不到」**的返回，里面没有这两个标记 → 闸门判「数据就绪」放行。实测 `600519 [market,fundamentals]` 拿到的是 `"Profile fundamentals **are** withheld for this date…"` 和 `"Balance Sheet data is withheld for this date…"`，一条财务数据没有，照样放行去写决策 | 增加 6 条词组正则（`is/are/was/were withheld`、`not a US SEC filer`、`no (matching) news/data`、`unable to fetch` 等）。⚠️ 词形要全：早前只写 `is withheld`，而实测返回的是**复数 `are withheld`**，改了仍漏判。正例 12/12、误伤实测 3/3（真实行情 2099B / MACD 1558B / RSI 1567B 均放行） |

**口径修正**（早前文档写错，已更正）：Twelve Data 日线官方默认
`adjust=splits`（**按拆股调整、不含分红**），不是「不复权」；
`outputsize` 官方上限 **5000**，不是无上限。

---

## 数据闸门（`tools_probe/preflight.py`，非上游改动）

分析师拿不到数据时**不抛异常**，工具只返回 `DATA_UNAVAILABLE:` /
`NO_DATA_AVAILABLE:` 文字（`dataflows/router.py:176-197`），分析师会照常往下走到
research/trader/risk/portfolio，用模型先验知识编造数字。

⚠️ **只搜这两个标记是不够的**（台账 #11）。upstream 还有一类「不报错但也没数据」
的返回：一段正常的说明文字，内容是「这份数据被 withheld / 取不到」，没有上述标记。
检测改为「标记 + 6 条词组正则」，见台账 #11 的实测原文。

`run.sh` 在启动分析前先跑闸门：对本次启用的分析师逐个真实调用其关键工具，
检测无数据标记，缺就 **exit 2 中止，不进入决策阶段**。

闸门用 `get_stock_data`（**在线路径，不读缓存**）探测，比缓存更严格，
源一挂就拦。这是**失败保守**方向，符合决策闸门的要求。

### 验证结果

| 场景 | 退出码 | 结论 |
|---|---|---|
| 600519 [market] | 0 | 放行 |
| 600519 [market,news] | 1 | 停止（新闻仍走 yfinance） |
| 600519 [market,social] | 1 | 停止（social 用 `get_news`，仍走 yfinance） |
| 600519 [market,sentiment] | 1 | 停止（**非法分析师名，不再跳过**） |
| 600519 [market,fundamentals] | 1 | 停止（A 股无财报源；**修 #11 前的实测返回 0 —— 漏判**） |
| AAPL [market]（无 key） | 1 | 停止（回退 yfinance 撞 429） |

> ⚠️ **A 股目前只能 `--analysts market`**：news / social / fundamentals 三类数据
> 在 A 股本就没有数据源，闸门会按设计拦截。`run.sh` 的默认值已设为 `market`。

> 上表为 **2026-10-05 复测**结果（修完 #11 之后）。`market,fundamentals` 那一行
> 修前是 **0**——闸门把上游的 withheld 说明文字当成了「数据就绪」。

### 改检测规则前必做

`NO_DATA_PATTERNS` 是**词组匹配**，理论上可能误伤真实数据。改之前先跑一遍
「有数据」形态的实测，别只靠正例：

```bash
# 复用 preflight 自己的 _call / _no_data_hit，不复制逻辑
docker compose run --rm -T --entrypoint python -v "$PWD/tools_probe:/probe:ro" tradingagents - <<'PY'
import sys; sys.path.insert(0, "/probe")
from preflight import _call, _no_data_hit
from tradingagents.agents import tools as T
from datetime import date, timedelta
curr, start = date.today().isoformat(), (date.today()-timedelta(days=60)).isoformat()
for n, extra in [("get_stock_data", {}), ("get_indicators", {"indicator":"macd"})]:
    out = _call(getattr(T, n), "600519", curr, start_date=start, **extra)
    print(f"  {_no_data_hit(out) or '放行'}  {n}  {len(out)}B")
PY
```

预期：两行都「放行」。**任何一行命中就是误伤**，得收窄规则。

---

## 验收证据

| 标的 | 路由 | 行情 | 指标 | snapshot | 结算 | snapshot 收盘 vs 行情 |
|---|---|---|---|---|---|---|
| 600519 | 腾讯 | 640 行 | ✅ | ✅ | 640 点 | 1,258.62 = 1,258.62（容差 0） |
| 000001.SZ | 腾讯 | ✅ | ✅ | ✅ | ✅ | 逐位一致 |
| AAPL | Twelve Data | ✅ | ✅ | ✅ | ✅ | 逐位一致（源报 `adjust=splits`） |

**证据范围**：验证的是**同一时点、同一配置下四条路径返回的数值一致**。
**不**验证数据源本身的口径正确性（那需要对照供应商原始响应单独做）。

端到端（600519，409s，闸门放行）报告与缓存逐位对账：
收盘 1,258.62 / 最新交易日 2026-09-30 / 成交量 3,833,100 股（= 38,331 手 ×100）。

---

## 配置

| 变量 | 取值 | 说明 |
|---|---|---|
| `TRADINGAGENTS_PRICE_SOURCE` | `auto`（默认）/ `tencent` / `twelvedata` / `yfinance` | `auto` = A 股走腾讯、美股走 Twelve Data。`yfinance` 可完全恢复 upstream 原行为，用于对照 |
| `TWELVEDATA_API_KEY` | 正式免费 key | **只放服务器 `.env`，不入库**。未配置时美股无法取数 |
| `TRADINGAGENTS_PRICE_FALLBACK` | 默认关闭 | 设为 `yfinance` 打开回退 |
| `TRADINGAGENTS_PRICE_FALLBACK_CYCLES` | `3` | 连续失败 N 次后熔断 |

腾讯接口**无需 key**。

---

## 恢复 / 卸载

```bash
cd ~/tradingagents-deploy/TradingAgents
git apply --check ../patches/0001-pluggable-price-feed.patch   # 确认能回滚
git checkout -- tradingagents/dataflows/vendors/yahoo/
rm -f tradingagents/dataflows/price_feed.py
```

完全恢复后，行情会退回「Yahoo 429 全线不可用」的原始状态。
缓存文件 `{symbol}-{source}-data.csv` 留着无害，删掉即可回退到旧命名。

---

## 尚未适配的能力（**不要误以为已完全摆脱 Yahoo**）

| 能力 | 当前状态 |
|---|---|
| 日线行情 / 技术指标 / snapshot / 结算 | ✅ 已适配（腾讯 + Twelve Data），含区间过滤与实际来源缓存 |
| 财报三表 | ⚠️ 美股走 SEC EDGAR（本来就通，但只覆盖三张报表，`get_fundamentals` 的 vendor 表里没有它）；**A 股无源** |
| 新闻 `get_news` | ❌ 仍走 yfinance（`yahoo/news.py:82`）→ 429 |
| 公司概览 / 内幕交易 | ❌ 仍走 yfinance（`yahoo/fundamentals.py:98,140`）→ 429 |
| 公司身份 `get_company_profile` | ❌ 仍直连 yfinance → 429 |

> **换掉 `yf.Ticker().history()` 不等于摆脱了 yfinance。**
> 新闻走 `.get_news()`、财报走 `.info` / `.get_balance_sheet()`，
> 是完全不同的方法，不在补丁覆盖范围内。
