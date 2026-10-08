#!/usr/bin/env bash
# 触发一次 TradingAgents 分析（readme §15）
#
# 用法:
#   ./scripts/run.sh 600519                    # 今天，只跑技术面（默认）
#   ./scripts/run.sh AAPL 2026-09-29           # 指定分析日
#   ./scripts/run.sh 600519 "" market,news     # 显式指定分析师组合
# 退出码: 0 成功 / 1 环境或配置问题 / 2 数据闸门拦截（缺关键数据，未进入决策）
#
# 行为（对应 readme §6 / §13）:
#   - 全局互斥锁，强制 max 1 concurrent job（2C2G 机器的硬约束）
#   - 记录 start / end / status / error，绝不记录 API key
#   - 日志按大小轮转，禁止无限增长
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

TICKER="${1:?用法: $0 <TICKER> [YYYY-MM-DD] [analysts,逗号分隔]}"
TRADE_DATE="${2:-$(date +%F)}"
# --analysts 在非交互模式下是【必填】而非选填：upstream 的 unattended_gaps()
# (cli/selections.py:58) 会检查它，缺了就直接报错退出而不是给默认值。
# 默认只跑技术面。默认值跟随【当前实际可用的数据源】，不是 upstream 的全集：
# news / social / fundamentals 在 A 股没有数据源（新闻与情绪走 yfinance、
# 财报只覆盖 SEC filer），配上它们会被下面的数据闸门按设计拦下（exit 2）。
# 等 A 股的新闻/财报源接上后，这里可以再放宽。
# 合法值取自 cli/models.py 的 AnalystType：market / social / news / fundamentals。
# 注意情绪分析师的内部值是 social（界面标签才是 "Sentiment Analyst"）。
ANALYSTS="${3:-market}"
LOCK_DIR=".run.lock"
LOG_DIR="logs"
LOG_FILE="${LOG_DIR}/run-$(date +%Y%m%d).log"
LOG_MAX_BYTES=$((5 * 1024 * 1024))
LOG_KEEP=5

# --- 日志轮转：超过阈值就压成 .1，删掉最老的 ---
rotate_log() {
  [[ -f "$LOG_FILE" ]] || return 0
  local size
  size=$(wc -c <"$LOG_FILE" | tr -d ' ')
  [[ "$size" -lt "$LOG_MAX_BYTES" ]] && return 0
  rm -f "${LOG_FILE}.$((LOG_KEEP - 1))"
  for ((i = LOG_KEEP - 2; i >= 1; i--)); do
    [[ -f "${LOG_FILE}.$i" ]] && mv "${LOG_FILE}.$i" "${LOG_FILE}.$((i + 1))"
  done
  mv "$LOG_FILE" "${LOG_FILE}.1"
}

log() {
  printf '%s [%s] %s\n' "$(date +'%F %T')" "$TICKER" "$*" | tee -a "$LOG_FILE"
}

# --- 单任务互斥：目录锁比文件锁更简单，崩了也自动释放 ---
acquire_lock() {
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "ERROR 已有分析任务在运行（锁：$LOCK_DIR）。2C2G 机器禁止并发，先等上一个跑完。"
    log "      如果确认没有任务在跑，手动清理：rmdir $LOCK_DIR"
    exit 1
  fi
  # 无论正常退出还是被 Ctrl+C / kill，都释放锁
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM
}

mkdir -p "$LOG_DIR" data
rotate_log

# --- 前置检查：.env 必须存在且 LLM 配置不能是空值 ---
if [[ ! -f .env ]]; then
  log "ERROR 找不到 .env。执行: cp .env.example .env && chmod 600 .env，然后填 LLM 配置"
  exit 1
fi

# 拆成三行：directive 只能挂在【下一行是单条命令】时才生效。
# 写成 `set -a; source .env; set +a` 的话，上一行的 disable 覆盖不到复合
# 命令里的 source（SC1091 照报），行尾写 directive 又会触发 SC1126。
set -a
# shellcheck disable=SC1091
source .env
set +a

missing=()
[[ -z "${TRADINGAGENTS_QUICK_THINK_LLM:-}" ]] && missing+=("TRADINGAGENTS_QUICK_THINK_LLM")
[[ -z "${TRADINGAGENTS_DEEP_THINK_LLM:-}" ]] && missing+=("TRADINGAGENTS_DEEP_THINK_LLM")
[[ "${TRADINGAGENTS_LLM_PROVIDER:-}" == "openai_compatible" \
   && -z "${TRADINGAGENTS_LLM_BACKEND_URL:-}" ]] && missing+=("TRADINGAGENTS_LLM_BACKEND_URL")
if (( ${#missing[@]} > 0 )); then
  log "ERROR .env 里这几项还没填：${missing[*]}"
  exit 1
fi

acquire_lock

log "START ticker=${TICKER} date=${TRADE_DATE} analysts=${ANALYSTS} provider=${TRADINGAGENTS_LLM_PROVIDER:-unset} model=${TRADINGAGENTS_QUICK_THINK_LLM:-unset}"

# 非交互模式的完整参数契约见 cli/selections.py:55-68 (unattended_gaps)：
#   flags 必须齐全 --ticker --date --analysts --save/--no-save --show/--no-show
#   env   必须齐全 OUTPUT_LANGUAGE / MAX_DEBATE_ROUNDS+MAX_RISK_ROUNDS
#                    / LLM_PROVIDER / QUICK_THINK_LLM 或 DEEP_THINK_LLM
# 少任何一个，upstream 都会在无 TTY 下打印 "No terminal to answer the setup
# questions" 并 exit 1（cli/run.py:101-104）。上面已逐一校验 env 部分。
args=(--ticker "$TICKER" --date "$TRADE_DATE" --analysts "$ANALYSTS"
      --save --no-show)

# ---- 数据闸门：关键数据缺失就不进入决策阶段 ----
# 分析师拿不到数据时工具只返回 DATA_UNAVAILABLE 文字而不抛异常，分析器会
# 照常往下走到 research/trader/risk/portfolio，用模型先验知识编造数字。
# 这里先真实调一次本次启用的分析师要用的工具，缺就中止。
echo "--- 数据闸门 ---"
# 显式取退出码而不是靠 pipefail：管道里 tee 成功会把状态盖成 0，
# 闸门就形同虚设了。
GATE_OUT=$(docker compose run --rm -T --entrypoint python \
  -v "$PWD/tools_probe:/probe:ro" tradingagents \
  /probe/preflight.py --ticker "$TICKER" --analysts "$ANALYSTS" --date "$TRADE_DATE" 2>&1) && GATE_RC=0 || GATE_RC=$?
printf '%s\n' "$GATE_OUT" | tee -a "$LOG_FILE"
if (( GATE_RC != 0 )); then
  log "STOP 数据闸门拦截（退出码 ${GATE_RC}）：关键数据缺失，已中止，未进入决策阶段"
  exit 2
fi
echo

START_TS=$(date +%s)
# -T 关掉 TTY 分配：这样 .env 缺 key 时 upstream 会直接报错退出
# (cli/prompts.py:653 检查 isatty)，而不是卡在交互提问上等输入。
if docker compose run --rm -T tradingagents "${args[@]}" 2>&1 | tee -a "$LOG_FILE"; then
  ELAPSED=$(( $(date +%s) - START_TS ))
  log "OK    完成，耗时 ${ELAPSED}s"
  log "报告: data/logs/${TICKER}/${TRADE_DATE}/reports/"
  exit 0
else
  STATUS=$?
  ELAPSED=$(( $(date +%s) - START_TS ))
  log "FAIL  退出码 ${STATUS}，耗时 ${ELAPSED}s"
  # checkpoint 已开的话，直接重跑同一条命令会从中断处继续，不必从头再来
  log "      重跑同一条命令即可从 checkpoint 续跑"
  exit "$STATUS"
fi
