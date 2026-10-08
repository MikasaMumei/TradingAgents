#!/usr/bin/env bash
# 更新 upstream 并重建镜像（readme §19：尽量不改动上游代码，靠拉取升级）
#
# 用法: ./scripts/update.sh [--dry-run]
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

UPSTREAM="TradingAgents"
CURRENT_BEFORE=$(cd "$UPSTREAM" && git rev-parse --short HEAD)

echo "==> 当前 upstream: ${CURRENT_BEFORE}"

if (( DRY_RUN )); then
  echo "==> dry-run，只看会拉到什么，不改动任何东西"
  (cd "$UPSTREAM" && git fetch --dry-run 2>&1 | head -20)
  (cd "$UPSTREAM" && git log --oneline HEAD..origin/main 2>/dev/null | head -20) || true
  exit 0
fi

# 有任务在跑就不要动，避免重建镜像时把正在用的镜像换掉
if [[ -d .run.lock ]]; then
  echo "ERROR 有分析任务正在运行（锁：.run.lock），等它跑完再更新。" >&2
  exit 1
fi

echo "==> 拉取 upstream"
(cd "$UPSTREAM" && git pull --ff-only)

CURRENT_AFTER=$(cd "$UPSTREAM" && git rev-parse --short HEAD)
echo "==> 更新到: ${CURRENT_AFTER}"

if [[ "$CURRENT_BEFORE" == "$CURRENT_AFTER" ]]; then
  echo "==> 无变化，无需重建"
  exit 0
fi

echo "==> 重建镜像（1.9Gi 内存机器上这一步最吃资源，耐心等）"
docker compose build

echo
echo "==> 更新完成。请跑一次 ./scripts/healthcheck.sh 确认，然后用一个低深度任务验证："
echo "    ./scripts/run.sh AAPL \"\" market"
