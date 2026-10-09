# 四股真实运行验收（2026-10-09）

状态：四股诊断、SpaceX与茅台修复重跑均完成；最后追加的缓存锁已经离线及免费并发取数验证。全部完整运行通过阶段闸门，但内容审阅仍发现口径和解释问题，不能称所有模型结论可靠。

运行仓库为 Windows `D:\TradingAgents`，venv 为 `.venv`。上游基线为 v0.6.0 / `1394a3f`，已提交并推到 fork 的初始维护基线为 `ce319ff`，分支为 `fix/windows-runtime-guards`。本页记录各批次生成时的源码状态；后续0003–0005交付已获授权，实际提交/推送以Git历史和[交付清单](pending-git-delivery-20261009.md)为准，不改写旧批次源码哈希。

## 标的与配置

| 市场 | 公司 | 项目代码 | 分析师 |
|---|---|---|---|
| 美股 | Apple | AAPL | market,sentiment,news,fundamentals |
| 美股 | SpaceX | SPCX | market,sentiment,news,fundamentals |
| A股 | 贵州茅台 | 600519.SS | market,fundamentals |
| A股 | 中国海油 | 600938.SS | market,fundamentals |

SpaceX 的公司名称和 SPCX 已在 [SEC 10-Q](https://www.sec.gov/Archives/edgar/data/1181412/000162828026052535/spcx-20260630.htm) 核对；中国海油的 A 股代码 600938 已在[公司股票信息](https://www.cnoocltd.com/english/investorrelations/stockinformation/)核对。实际 Yahoo 公司身份同时匹配，未使用同名 ETF 代替 SpaceX。

分析日为 2026-10-09。模型保持 `MiniMax-M3.1-Flash-Preview`，上下层相同；max_tokens=32768，研究与风险各两轮，模型 SDK retries=6，整次失败重跑=0。受保护入口显式使用 `--effort low`，不修改项目密钥、模型或全局 Desktop 配置。各批使用独立缓存、日志、报告、记忆和新 checkpoint，不覆盖之前的失败证据。

诊断批次：`D:\TradingAgents\data\validation\acceptance-20261009-dated`。`batch.json`、`source-hashes.json`、`source-diff.patch`、`run-status.json` 记录配置、代码与实际运行；每股保存免费数据 preflight、原始多行财报、确定性摘要、模型调用统计和独立回调观测。四股共83次调用，没有空回复、length截断或回调漏记；304个原有报告/日志文件没有变化。52个报告产物已逐文件校验后复制至Mac的`reports/acceptance-20261009-dated/`。

| 标的 | 真实调用 | 完整阶段 | schema成功 / 失败保留原文 | 阶段闸门 | 内容边界 |
|---|---:|---:|---:|---|---|
| AAPL | 21 | 12/12 | 1 / 3 | ACCEPT | 主要财务值对齐，部分累计营收和因果解释仍缺证据 |
| SPCX | 25 | 12/12 | 1 / 3 | ACCEPT | 原报告使用不足200日的均线，内容验收不通过，等待重跑 |
| 600519.SS | 18 | 10/10 | 0 / 3 | ACCEPT | 原报告未经期间证明合并现金流，等待重跑 |
| 600938.SS | 19 | 10/10 | 1 / 2 | ACCEPT | 年报替代明确；工具缺季度数据不能推断公司没有披露 |

阶段闸门的ACCEPT只证明所需阶段与工具证据到位，不能自动检测所有数值口径和解释错误。

中国海油季度现金流不可用、季度损益表缺营收与净利行；年度损益表和现金流可用。允许明确标为年度的分析，禁止用年度值填季度/H1。SpaceX 上市后的价格历史不足 200 个交易日，不能编造完整 200 日指标或上市前股价。

## 本轮实际发现与修复

1. 默认推理预算下，苹果基本面的一次真实回复 `finish_reason=length`，32768 个 completion tokens 全用于推理，可见正文为空。该批已停止，保留失败证据。相同模型、相同上限改用 low 后未再观察到该症状；这只支持该配置的实测结论，不保证任何模型永不截断。
2. 真正的工具调用通过 `InjectedState` 提供 ticker；AIMessage 参数中不含 ticker。旧摘要将其当作不匹配，跳过全部财务摘要。修复为：显式 ticker 必须匹配，缺参时继续以 matched tool_call_id、工具种类、报表标题、分析日和未来期间检查约束本轮证据。新增真实 LangGraph 子图的工具循环回归，在旧源码失败、修复后通过。
3. Rich 会把长报告路径换行，验收脚本从显示文字读到截断的 `...\repo` 路径，误拒绝已生成报告。修复为保存入口返回 Path，经 CLI callback 直接交给验收器；保存失败/跳过返回 None，不找磁盘上其他旧报告替代。回归覆盖带空格的长路径和保存失败。
4. 注入最新财季锚点后，模型仍会错配历史列、补不存在的季度。修复为财务摘要同时提供最近八个来源列的“期间＋字段名＋值”，覆盖损益、现金流和资产负债表，保留累计期间、原始单位和缺失值；提示词要求不填缺失季度。它约束数字来源，不能替代对模型因果推断的审阅。
5. 真实router和记忆结算在guard安装前已经保存了原函数引用，导致其中一条价格路径仍带入美股未收盘的10月8日行情。修复安装时同步替换仍指向原函数的引用，保留其他自定义覆盖；新增子进程真实路由回归，旧源码失败、修复通过。
6. stockstats和公司metadata可返回部分窗口均线，SpaceX只有81个已收盘交易日，却获得了“200日均线”。50/200日SMA现要求完整有效窗口，单值、批量、snapshot一致返回N/A；移除基本面metadata的均线字段，避免旁路覆盖。
7. 茅台现金流只有列结束日，无开始日或明确期间长度。确定性计算现仅在明确三个月列时生成日历半年合计；未知期间只列原值并提醒禁止合并。不能仅凭供应商的quarterly标题认定每列都是三个月。
8. SPCX重跑的6个指标拒绝均有同秒WinError5/32证据：多个工具线程刷新同一cache时互相占用文件。追加同一缓存路径的可重入线程锁，让核心读写和guard刷新共用一个事务；不改变缺数据拒绝规则。真实报告保留修复前的partial状态，追加修复通过独立离线与免费并发取数验证，不伪称那份报告已在缓存锁版本重跑。

失败批次均保留：`acceptance-20261008T153728Z`、`acceptance-20261008T153728Z-low`、`acceptance-20261009-fixed`。它们分别记录空回复、显示路径误拒绝/原摘要未注入、最新锚点已注入但历史仍错配；不可作为成功报告。

## 代码和补丁验证

- Windows 全仓 Ruff：通过。
- 最终Windows源码完整pytest：1301 passed，4 skipped，1 deselected，99 subtests passed，20 warnings。缓存锁前为1299项，新增2项并发回归在旧源码均失败（同时进入6个下载），修复后通过。跳过项为三项POSIX文件模式与未安装的可选Bedrock依赖，没有为此安装依赖。第一次以PYTHONIOENCODING单独设UTF-8运行时，一项CLI子进程测试因父子编码不一致失败；同时启用PYTHONUTF8=1后完整复核通过。失败日志仍保留。
- 最终修改后5组相关离线验收脚本通过；此前0003首修后10组既有脚本通过。未批量运行可能联网的所有探针。
- [0003 补丁](../patches/0003-injected-financial-context-report-path.patch)：基于ce319ff，7个路径，23116 bytes，SHA256 `b7e56432bb1faf612852cadfe31a0b8ab27701927cb409f2d4baaeb31739eca4`。
- [0004 补丁](../patches/0004-settled-market-full-window.patch)：基于ce319ff，7个路径，16857 bytes，SHA256 `b38ae8c2576b95a901bb97e7029c938df4701c348b0db699593ee5e61bc98573`。
- 两份补丁在干净ce319ff上分别`git apply --check`并顺序应用，86项相关回归通过，14个源文件文本与实际运行源码一致。Git应用后的CRLF/LF字节可能不同，不能宣称逐字节相同。
- 修复前源码 ZIP、干净基线 archive、补丁验证和完整测试日志在 SSH 用户 `%LOCALAPPDATA%\tradingagents-audit\context-20261009`。没有覆盖用户的其他改动。

四股免费真实行情核验：`data/validation/market-final-20261009-v2/results.json`。router、snapshot和memory settlement的最后日期一致、收盘价精确到分一致。美股最新已收盘行为10月7日，A股为10月8日；SPCX仅81行，200日SMA明确不可用，50日SMA为142.2702。单值指标按实际最后交易日查询，未将尚未来临的分析日当作已有日线。最初探针误用分析日查询单值而收到“无交易行”，该探针失败证据保留，修改探针日期后重查通过。

## 最终修复重跑

批次`D:\TradingAgents\data\validation\acceptance-20261009-final`使用0003/0004的现金流期间、已收盘过滤和完整SMA窗口修复，只对受到明确口径错误影响的SPCX、600519.SS重新执行付费完整分析。缓存线程锁于报告完成后追加并单独验证。各版本以批次source-hashes.json及私有audit为准。AAPL、中国海油此前完整运行和四股免费价格核验均保留，不能宣称它们也在最终源码上重跑过完整模型流程。

| 标的 | 调用 | 阶段 | schema成功 / 失败保留原文 | 报告文件夹 |
|---|---:|---:|---:|---|
| SPCX | 21 | 12/12 | 1 / 3 | SPCX_20261009_020844 |
| 600519.SS | 19 | 10/10 | 0 / 3 | 600519.SS_20261009_021554 |

两股共40次调用，没有空回复/length截断，回调开始/结束40/40，无观测错误。304个原有报告/日志仍未变化。报告与免费数据证据38个文件已逐文件校验复制至Mac `reports/acceptance-20261009-final/`；模型原文未改写。

SPCX市场报告明确200日SMA不可用，使用10月7日收盘167.60；主要财务金额和六个月现金流口径保留。6个指标请求因缓存刷新拒绝，snapshot及在线行情提供当前证据，market为partial，阶段闸门仍ACCEPT。后加缓存锁的免费8指标并发取数全部成功、degraded_count=0，结果在`data/validation/cache-parallel-final-20261009/results.json`，不是再次完整模型运行。

SPCX新报告仍把“现金减流动负债”命名为净现金72400 USD million，缺少债务定义，部分融资/非经营损益的原因解释也缺证据。茅台现金流表没有再半年合计，但正文仍称某列为Q2单季，单位断言和Other Receivables的业务解释亦未经来源确认。详细错误已写在Mac报告的review-note.md；原始报告不改写。技术修复已验证，整个报告的内容验收尚不能判为通过。

本项目可以生成完整研究报告；财务派生定义、期间标签和自然语言解释仍需审阅。随后已实现0005语义约束与有限筛查，具体边界见下节；[DSH只读审阅prompt](dsh-handoff-20261009.md)保持现有协作权限。精确提交/推送范围见[Git交付清单](pending-git-delivery-20261009.md)。

## 当前内容审阅

2026-10-09后续已经实现0005财务语义约束与有限筛查，并同步Windows。旧SPCX/茅台报告离线重放均标REVIEW，原文未改写；私有副本完整测试1336项通过，干净0003→0004→0005补丁链199项通过。这不是0005之后的新模型完整运行，仍不能把上述旧报告视为已修正。实现、数据范围与DSH审阅见[财务语义说明](financial-semantics-20261009.md)。

苹果新基本面报告的八列损益数据、最新资产负债数据、累计现金流和日期已与来源对齐：最新营收109417、毛利54770、营业利润35695、净利29789（USD million）、EPS2.02 USD/share、毛利率50.06%、净利润同比27.12%。报告已明确缺少2024-09-28、2025-09-27季度损益列。

仍须区分事实与解释：报告声称历史四季度营收326240，却没有可重算的连续四季来源；产品组合/营运资本改善等因果判断缺直接字段支持。TTM FCF107721875456 USD来自另一个 `get_fundamentals` 工具，不是 SEC现金流表的 FCF 行，已保存独立核对文本 `AAPL/get_fundamentals-audit.txt`。DSH只读取损益表/现金流表时对该值“无工具证据”的判断超出其读取范围，不能据此断定该数伪造。

DSH 只读交叉审阅：第一次 `20261008T172551Z-b3ed8a97` 达4096输出上限且无可见回复，属于失败；第二次 `20261008T172728Z-1486eeff` 在同一会话恢复已读文件后，非空回复、turn completed、idle、退出0，确认主要数字/期间和两处代码限制。没有执行测试或修改源码；DSH回复不能代替真实运行验收。

## 后续手动启动入口

在 Windows 项目目录使用已有 `.env` 和 venv，例如：

```powershell
Set-Location D:\TradingAgents
.\.venv\Scripts\python.exe tools_probe\guarded_run.py --effort low --max-retries 0 -- --ticker AAPL --date (Get-Date -Format yyyy-MM-dd) --analysts market,sentiment,news,fundamentals --save --no-show --html
```

A股将代码替换为 `600519.SS` 或 `600938.SS`，分析师使用 `market,fundamentals`。SpaceX 使用 `SPCX`。阶段完整、数据有证据和财务解释正确分别验收；本项目的阶段闸门不能自动证明所有自然语言结论正确。
