#!/usr/bin/env bash
# 部署自检：一眼看出哪里没配好（readme §22 DoD 的日常巡检）
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

# 三个打印函数都显式 return 0：下面大量使用 `cond && ok || bad` 的写法，
# 若 ok 自身返回非零会连带触发 bad，误报成失败。
fail=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; return 0; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; fail=1; return 0; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; return 0; }

echo "== 1. 基础环境 =="
command -v docker >/dev/null && ok "docker $(docker --version 2>/dev/null | cut -d' ' -f3 | tr -d ',')" \
  || bad "docker 没装"
docker compose version >/dev/null 2>&1 && ok "compose $(docker compose version --short 2>/dev/null)" \
  || bad "docker compose v2 不可用"

echo "== 2. .env 配置 =="
if [[ -f .env ]]; then
  ok ".env 存在（权限 $(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env 2>/dev/null)）"
  [[ "$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env 2>/dev/null)" == "600" ]] \
    || warn ".env 权限不是 600，建议 chmod 600 .env"
  # shellcheck disable=SC1091
  set -a; source .env; set +a
  [[ -n "${TRADINGAGENTS_QUICK_THINK_LLM:-}" ]] && ok "quick 模型: ${TRADINGAGENTS_QUICK_THINK_LLM}" \
    || bad "TRADINGAGENTS_QUICK_THINK_LLM 没填"
  [[ -n "${TRADINGAGENTS_DEEP_THINK_LLM:-}" ]] && ok "deep 模型: ${TRADINGAGENTS_DEEP_THINK_LLM}" \
    || bad "TRADINGAGENTS_DEEP_THINK_LLM 没填"
  echo "       provider: ${TRADINGAGENTS_LLM_PROVIDER:-<未设>}"
  [[ "${TRADINGAGENTS_LLM_PROVIDER:-}" == "openai_compatible" ]] && {
    [[ -n "${TRADINGAGENTS_LLM_BACKEND_URL:-}" ]] && ok "backend_url: ${TRADINGAGENTS_LLM_BACKEND_URL}" \
      || bad "openai_compatible 必须填 TRADINGAGENTS_LLM_BACKEND_URL"
  }
  [[ "${TRADINGAGENTS_CHECKPOINT_ENABLED:-}" == "true" ]] && ok "checkpoint 已开启" \
    || warn "checkpoint 未开启，中断后要从头重跑"
else
  bad ".env 不存在：cp .env.example .env"
fi

echo "== 3. 镜像 =="
# 注意：不能用 `docker compose images` 判断镜像是否构建。
# 那个命令是「按容器」列镜像的，而本项目所有任务都用 --rm 跑，
# 容器跑完即销毁，列表永远是空的，会把已构建的镜像误报成缺失。
# 镜像名从 compose 配置里读，避免升级改 tag 后这里写死失配。
IMAGE_NAME=$(docker compose config 2>/dev/null | grep -oP '^\s+image:\s+\K\S+' | head -1)
if [[ -n "$IMAGE_NAME" ]] && docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
  # 体积用 `docker images` 读，不要用 `docker image inspect .Size`：
  # buildkit 产出的是带 attestation 的 manifest list，inspect 报的 Size
  # 不是展开后的实际占用，会小得离谱。
  ok "镜像已构建: ${IMAGE_NAME} ($(docker images "$IMAGE_NAME" --format '{{.Size}}' | head -1))"
else
  bad "镜像缺失（${IMAGE_NAME:-未从 compose 解析到}）：docker compose build"
fi

echo "== 4. 持久化数据 =="
# 路径必须跟 upstream default_config 的默认值一致，改这里前先看
# TradingAgents/tradingagents/default_config.py:80-82：
#   results_dir     = ~/.tradingagents/logs
#   data_cache_dir  = ~/.tradingagents/cache   <- checkpoint 在这个下面
#   memory_log_path = ~/.tradingagents/memory/trading_memory.md
# checkpoint 跟着 data_cache_dir 走，历史上有人误以为它在 ~/.tradingagents/checkpoints
for d in data/logs data/cache data/cache/checkpoints data/memory; do
  [[ -d "$d" ]] && ok "$d 存在" || warn "$d 还不存在（跑一次分析后就会生成）"
done
n=$(find data/cache/checkpoints -name '*.db' 2>/dev/null | wc -l | tr -d ' ')
if [[ "$n" -gt 0 ]]; then
  ok "checkpoint 数据库: ${n} 个（中断后重跑同一条命令可续跑）"
else
  ok "checkpoint 数据库: 0 个"
fi

echo "== 5. 资源余量 =="
free -h 2>/dev/null | awk '/^Mem:/ {printf "       内存: 总量 %s  可用 %s\n", $2, $7}'
free -h 2>/dev/null | awk '/^Swap:/ {printf "       swap: 总量 %s  用了 %s\n", $2, $3}'
df -h / | awk 'NR==2 {gsub("%","",$5); printf "       磁盘: 用了 %s%%  可用 %s\n", $5, $4}'
avail=$(free -m 2>/dev/null | awk '/^Mem:/ {print $7}')
[[ -n "$avail" && "$avail" -lt 500 ]] && warn "可用内存不足 500MB，跑分析前先确认没有别的服务在抢" || true

echo "== 6. 任务锁 =="
[[ -d .run.lock ]] && warn "有任务正在运行（或上次异常退出没清锁：rmdir .run.lock）" || ok "无运行中的任务"

echo
if (( fail )); then
  echo "结论: 有必须修的问题，见上面标 ✗ 的行"
  exit 1
fi

# 结论要按实际数据源可用性给，不能笼统说"可以跑"。
# 美股 key 没配时，硬说"可以跑 AAPL"会误导 —— 分析会被数据闸门拦下。
if [[ -z "${TWELVEDATA_API_KEY:-}" ]]; then
  echo "结论: A 股行情可用；**美股不可用**（未配 TWELVEDATA_API_KEY）"
  echo "  A 股:  ./scripts/run.sh 600519 \"\" market"
  echo "  美股:  先填 .env 的 TWELVEDATA_API_KEY，否则数据闸门会拦截"
  exit 0
fi
echo "结论: 双市场行情均可用"
echo "  示例:  ./scripts/run.sh 600519 \"\" market   # A 股"
echo "        ./scripts/run.sh AAPL \"\" market     # 美股"
