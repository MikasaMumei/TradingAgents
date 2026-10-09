# Git交付清单

当前Windows运行版本已经应用0003、0004、0005，源码、测试和文档已整理并验证。用户于2026-10-09授权这29个文件的提交交付。本文件记录执行清单；最终提交SHA和推送核验以Git历史及独立交付记录为准，列出命令本身不代表执行成功。最终真实运行结果见[四股验收](four-stock-acceptance-20261009.md)，后续语义约束的离线验证与DSH审阅见[财务语义说明](financial-semantics-20261009.md)。

目标：Windows `D:\TradingAgents`，分支`fix/windows-runtime-guards`，父提交`ce319ffcbe57af6d68ac8acd517e5088cd66619c`。只推送用户fork `https://github.com/MikasaMumei/TradingAgents.git` 的同名功能分支，不推上游、不合并主分支。

项目 `D:\TradingAgents\AGENTS.md` 明确要求：“commit/push 必须经用户明确授权，先展示完整命令。”本轮授权对应此前展示的29文件提交及同名fork功能分支交付，不包括上游推送或合并主分支。

## Windows精确提交范围

```powershell
git -c safe.directory=D:/TradingAgents -C D:/TradingAgents add -- cli/main.py cli/run.py tools_probe/guarded_run.py tools_probe/exchange_sessions.py tools_probe/check_run_complete.py tradingagents/agents/analysts/fundamentals_analyst.py tradingagents/agents/context.py tradingagents/agents/rating.py tradingagents/dataflows/financial_metrics.py tradingagents/dataflows/vendors/yahoo/fundamentals.py tradingagents/dataflows/vendors/yahoo/market.py tradingagents/dataflows/vendors/yahoo/ohlcv.py tradingagents/dataflows/vendors/yahoo/snapshot.py tests/test_cli_headless.py tests/test_financial_metrics.py tests/test_financial_semantics.py tests/test_graph_end_to_end.py tests/test_fundamentals_units.py tests/test_indicator_history.py patches/0003-injected-financial-context-report-path.patch patches/0004-settled-market-full-window.patch patches/0005-financial-semantics-review.patch patches/README.md docs/windows-maintenance.md docs/four-stock-acceptance-20261009.md docs/financial-semantics-20261009.md docs/dsh-handoff-20261009.md docs/pending-git-delivery-20261009.md integrations/dsh-bridge/README.md
git -c safe.directory=D:/TradingAgents -C D:/TradingAgents diff --cached --check
git -c safe.directory=D:/TradingAgents -C D:/TradingAgents commit -m "Guard financial semantics and settled market analysis"
git -c safe.directory=D:/TradingAgents -C D:/TradingAgents bundle create C:/Users/local_ssh/AppData/Local/tradingagents-audit/context-20261009/financial-market-incremental.bundle refs/heads/fix/windows-runtime-guards ^ce319ffcbe57af6d68ac8acd517e5088cd66619c
```

提交前另核对：上述29个文件之外没有暂存改动；`.env`、真实报告、缓存、日志、DSH会话不在清单；源码哈希仍与最新离线验证/部署记录相同。不能把旧完整模型批次的source-hashes更新成0005源码。已有改动不覆盖，bundle使用尚不存在的文件。源码快照和最终完整待交付diff另保存在SSH用户私有audit目录。

## Mac现有登录身份中转推送

Windows专用SSH账号无法使用桌面用户的Git Credential Manager。沿用上次成功的Mac中转裸仓库，传回仅包含Git对象的bundle，不传回Windows凭据。把bundle传入以下精确路径后，执行：

```sh
git -C /var/folders/vf/7g68_djs6j93zbrywjx1_vvm0000gn/T/tradingagents-publish-20261008-xu1dtutr/repo.git fetch /var/folders/vf/7g68_djs6j93zbrywjx1_vvm0000gn/T/tradingagents-publish-20261008-xu1dtutr/financial-market-incremental.bundle refs/heads/fix/windows-runtime-guards:refs/heads/fix/windows-runtime-guards
git -C /var/folders/vf/7g68_djs6j93zbrywjx1_vvm0000gn/T/tradingagents-publish-20261008-xu1dtutr/repo.git push origin refs/heads/fix/windows-runtime-guards:refs/heads/fix/windows-runtime-guards
```

推送前比较中转仓库分支SHA与Windows新提交SHA，核对origin仍是上述用户fork。使用既有Mac登录，不更改全局凭据、SSH账号或安全配置。推送后重新读取远端分支SHA并由Windows fetch核对；推送失败则保留本地提交和bundle，明确记录失败原因。
