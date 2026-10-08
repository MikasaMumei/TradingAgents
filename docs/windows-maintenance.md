# Windows 维护与补丁说明

当前运行仓库是 Windows `D:\TradingAgents`，使用其 `.venv`，不需要 Docker。Mac 外层目录和其旧版嵌套 checkout 是历史云端工作区，不代表 Windows 的当前版本。

## Git 与源码边界

- 本次基线是上游 v0.6.0，HEAD `1394a3f`。`origin` 指向 `MikasaMumei/TradingAgents`，`upstream` 指向 `TauricResearch/TradingAgents`。
- 维护核心源码、`tools_probe/`、DSH 协作入口、测试及补丁必须进入功能分支；不要为了“保持上游干净”把源码目录写入 `.git/info/exclude`。
- `.env*`、真实持仓/自选配置、缓存、日志、运行报告及 DSH 会话记录不进入版本库。示例配置只能放占位值。
- `.git/info/exclude` 是本机规则，不能随 clone 传播；通用排除规则以仓库 `.gitignore` 为准。
- 过去未提交的脚本没有可恢复的逐次 Git 历史。首次提交只能记录当前交付，不补造历史。提交前的源码快照另保存在 SSH 用户 `%LOCALAPPDATA%\tradingagents-audit\20261008T124339Z`。
- 本次整理分支为 `fix/windows-runtime-guards`。commit/push 仍需用户明确授权；暂存区不等于已提交，也不等于已推到 fork。

## 当前修复

`0002-windows-runtime-guards.patch` 对应 1394a3f 的核心修复和可移植回归用例；在干净的该版本 checkout 中先 `git apply --check`，再应用。已修改的 Windows checkout 不要重复应用。完整自定义运行入口及旧工具的当前版本以功能分支源码为准。

结构化输出在绑定时请求 `include_raw=True`，保留同一调用的 `raw / parsed / parsing_error`。Research Manager、Trader、Sentiment Analyst 共享保留原文的路径；Portfolio Manager 同样处理原文，并保留成功解析时的 typed rating。只要首次调用有可见原文，就不额外请求模型。真正无原文时，日志明确记录追加调用是新决策、首次评级未知。旧适配器不支持原文接口时，直接采用一次普通文本调用。

财务摘要由核心 `financial_metrics.py` 计算，在基本面节点每轮创建提示词时读取本轮 `state.messages`。匹配工具调用 ID、ticker、分析日期和报表标题，拒绝未来期间和日期不一致的数据；后一次失败结果不会沿用前一次成功值。不读取共享 `message_tool.log`，不尝试把日志中被压平的 CSV 猜回报表。

摘要保留来源的金额单位；未声明币种和缩放单位时输出 `source units (currency/scale not stated)`，不凭股票代码猜测。YoY 按上一年同月、日期相差不超过七天且期间标签一致的唯一列比较；六个月累计按原列使用，明确结束日和“来源未提供开始日”，不称日历 H1。不足以证明可比单位时，不生成跨表 FCF/净利润比率。

`tools_probe/structured_guard.py`、`fin_context.py`、`fin_metrics.py` 只保留既有 runner 所需的兼容入口，不再维护第二套补丁实现。探针统计同时报告解析成功、解析失败和首次原文保留，不能把 envelope 存在当成解析成功。

## 离线验证

在项目目录使用已有环境：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_structured_reply.py tests/test_financial_metrics.py tests/test_structured_agents.py tests/test_structured_agent_prompts.py tests/test_portfolio_context.py tests/test_fundamentals_units.py tests/test_fundamentals_lookahead.py tests/test_rating_integrity.py
.\.venv\Scripts\python.exe tools_probe/test_fixes_integration_offline.py
.\.venv\Scripts\python.exe tools_probe/test_acceptance_scenarios.py
```

新增 fixture 是 2026-10-08 实际工具 CSV 的裁剪，保留原标题和所选列；用于校验解析与计算，不代表当前实时财务值，也不是模型生成的财报。AAPL 锚点为营收 109417 USD million、毛利率 50.06%、净利润 YoY 27.12%；茅台来源两季度 FCF 合计为 11523065222.10（2025）与 69858607366.78（2026），2025 OCF 为 13119061031.33。茅台原 CSV 未声明币种/缩放，摘要不得标为美元或把原数当成 millions。

修复集成测试和验收场景测试使用临时目录，不写真实运行日志。`test_acceptance_end_to_end.py` 是读取已有历史报告的审阅工具，缺历史报告时返回 2；其 runner 写入同样隔离到临时目录。其他探针有些会访问行情供应商；离线验证需明确选取 offline 用例，不能批量运行所有 `tools_probe/test_*.py` 后宣称没有联网。

上游缓存隔离测试在 Windows 上原本会误报：系统临时目录位于用户 home 之下。现在检查实际 yfinance 缓存是否为测试自有的独立临时目录，不改系统 TMP 或用户缓存配置。

## 尚未完成的运行验收

本次修复没有重新执行收费的 TradingAgents 完整分析，也没有更改模型、输出 token 上限、持仓或分析师范围。离线测试通过不等于新报告内容已经验收。

旧集成测试已经覆盖 2026-10-08 的 AAPL/茅台共享工具日志，当前分别只有 5786/7273 bytes。没有发现同路径的原始日志备份，不能宣称恢复了原始证据；旧报告与 manifest 的证据链仍有缺口。保留旧文件，后续新运行使用新 run ID 和独立证据，不修改历史结论。

下一步是提交当前源码，再分别执行 AAPL 与茅台的最小真实分析，审阅输出中的数值、期间、单位及首次决策日志。不要在这一轮混入 RSS 新闻接入、扩大分析师范围或重启云服务器。

## 历史云端层

`scripts/*.sh`、`patches/0001-pluggable-price-feed.patch` 和 `patches/price_feed.py` 保留 v0.5.2 云端方案的历史源码。它们使用旧版嵌套目录和 Docker 路径，不能作为 Windows venv 的启动/升级入口。旧云端资料见 [历史补丁说明](../patches/README.md)。

本次把自定义 Python 源码整理到现有全仓 Ruff 规则，移除两处内容完全相同的重复函数定义，统一导入和格式。`patches/price_feed.py` 副本也只做等价的格式/条件表达整理；恢复历史方案使用保持原文的 0001 patch，旧说明中的副本 MD5 仅描述历史快照，不再作为当前副本一致性校验。

DSH 固定协作会话保持只读，用于直接审阅与沟通；当前不能直接修改源码或替代测试证据。使用方式与实测边界见 [协作入口说明](../integrations/dsh-bridge/README.md)。
