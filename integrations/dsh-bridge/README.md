# DSH 协作入口

通过现有 `win-home` SSH 连接，调用 Windows 已安装的 DSH SDK 协议，向一个固定会话发送任务并读取回复。它独立于 Desktop 当前对话。

## 运行方式

Mac 和 Windows 使用相同的 `bridge.py` 源文件。Windows 副本放在 `D:\TradingAgents\integrations\dsh-bridge\bridge.py`，同目录必须有 `session-server.mjs`。不安装依赖、不修改 Desktop 配置、不开放网络端口。

协议依据：[官方 SDK 协议](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/protocol/README.md)和[官方 SDK 运行入口](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-app/README.md)。实际启动使用安装包自带的 0.2.0-rc.2 实现，不以滚动更新的上游文档代替运行验证。

该安装版原始 SDK 的 `session/prompt` 只会创建会话，同一 ID 跨进程调用会报 `session already exists`。本目录的 `session-server.mjs` 使用安装包公开的协议传输、`ctx.agents.resume()` 和 `ctx.agents.create()` 接口，保持 initialize / session/prompt / shutdown 的传输方式；先恢复既有会话，仅在明确的 `SessionPersistenceNotFoundError` 时新建，其他错误直接失败。不修改安装包、不通过拼接旧消息伪装恢复。[官方 Agent 接口说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent/README.md)

Mac 调用：

```sh
python3 /Users/chasexi/TradingAgents/integrations/dsh-bridge/bridge.py \
  --prompt-file /absolute/path/to/task.txt --timeout 240
```

首次调用的模型默认读取 Desktop 当前选择。用户明确选择其他模型时，在调用中添加 `--model MODEL_ID`。第一次成功往返后将选定路由记录在 Windows 独立运行目录的 `connection.json`，后续调用继续使用该模型，不会写回 Desktop 配置。模型必须出现在 Desktop 路由配置或现有网关的模型列表中。

## 配置与记录

- Windows 工作目录：`D:\TradingAgents`。
- DSH 命令：`D:\DeepSeeHarness\DSH\resources\runtime\cli\bin\dsh.cmd`，当前验证的安装版本为 `0.2.0-rc.2`。
- 会话 ID：`59f06a7b-6119-5929-b754-5449eea6bff9`。
- 独立 DSH home、协议事件与调用结果：SSH 用户的 `%LOCALAPPDATA%\dsh-coordination\tradingagents`。
- Mac 协议记录：本目录下的 `.runtime/`，已在本目录的 `.gitignore` 中排除。
- Desktop 配置来源：`C:\Users\thebl\.dsh\profiles\desktop\cordis.patch.yml`。仅选取其模型路由，凭据在 Windows 上按引用读取并传入临时子进程环境；不传回 Mac，不写入项目或独立 DSH home。
- 默认只读，关闭 shell、子 agent、网关管理及自动重试。DSH 返回的回复不等于项目已验证；仍需 Codex 检查实际文件和证据。
- 一个会话同时只允许一次调用。存在 `session.lock` 时拒绝进入，不自动删除可能仍在使用的锁。

## 完成判定

“接受消息”只说明任务已入队。“idle”或进程退出码 0 也不能单独证明任务成功。入口要求本轮结束原因为成功、有非空助手回复、进入 idle、进程退出码为 0。

超时或错误保留事件记录，停止本次桥接启动的进程，不影响 Desktop。实际执行错误和额度耗尽必须按失败处理。每次调用关闭运行进程，下次通过恢复接口继续使用同一会话 ID。会话 ID 和 workspace 均固定；恢复到其他工作目录时拒绝继续。

当前入口用于读取、审阅和沟通。代码修改、运行项目测试及依赖安装尚未通过这个入口验收；SDK 不提供完整的交互审批通道，不能把更宽权限作为默认配置。

当前协作进程的单次模型输出上限为 4096 tokens，适用于连通及短任务验证，不影响 TradingAgents 原有的输出配置。

## 当前验证情况

2026-10-08 已验证 SSH、DSH SDK 握手、只读策略及模型路由加载。首次 MiniMax 模型调用未获得有效回复。直接检查网关 SSE 后发现上游 `base_resp.status_code=2056`，提示 Token Plan 用量达到上限；外层为 HTTP 200，并附带用量帧，DSH 因而报 `Stream ended without finish_reason`。

第一个原型曾把该失败的 idle/退出码 0 误标为完成，原始记录保留，完成判定已修正。该记录不能作为连通成功证据。

额度恢复后，于 2026-10-08 20:08–20:09（北京时间）继续使用原模型 `MiniMax-M3.1-Flash-Preview`，完成以下实测：

| 验证 | 运行 ID | 结果 |
| --- | --- | --- |
| 有效往返 | `20261008T120855Z-e95bd1ff` | 回复指定暗号，turn/end=completed，进程退出码 0 |
| 关闭并重启进程后恢复 | `20261008T120911Z-642df761` | 同一 session ID；第二条 prompt 不含暗号，回复从历史恢复的正确暗号；恢复时有 43 条事件 |
| 真实文件读取 | `20261008T120951Z-f73d4278` | 实际调用 read 读取 README.md；标题第 30 行、News 第 32 行，与 Codex 独立读取一致 |

三次运行均产生非空助手回复、完成本轮、进入 idle、以退出码 0 结束。实际文件读取运行包含工具结果，不能只根据模型自述判断通过。

验证事件、任务正文及结果保存在上述 Windows 运行目录和 Mac 的 `.runtime/`。没有执行 TradingAgents 分析流水线，也没有修改业务代码。当前服务器实现基于已安装的 0.2.0-rc.2 API；DSH 升级后需要复查接口并重新验证，不承诺跨版本兼容。

代码验证包括 Python 语法、`ruff check`、`ruff format --check` 和 Node `--check`。桌面配置哈希在检查前后保持一致，未安装或升级依赖、未更改凭据及防火墙。

## 版本管理

Windows 仓库的 `git status` 已能看到 `integrations/`，`bridge.py` 未被忽略。当前文件尚未 commit 或 push，因此还没有提交历史；代码已放在可纳入仓库的位置，运行记录与凭据不纳入 Git。
