#!/usr/bin/env bash
# CD 部署脚本：在 Linux 服务器上运行（由 .github/workflows/cd.yml 经 SSH 调用，也可手动执行）。
# 流程：拉代码 → 同步依赖 → 解包 dist → 优雅停 agent → 重启服务 → 健康检查 → 失败回滚。
# 前提：服务器已 git clone 仓库、装好 uv、配好 systemd --user 服务（见 README「CD 持续部署」）。
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE="${DEPLOY_SERVICE:-llm-transaction}"
API_BASE="${DEPLOY_API_BASE:-http://127.0.0.1:17577}"
PREV_COMMIT="$(git rev-parse HEAD)"

echo "==> [1/6] 拉取最新代码（当前 $PREV_COMMIT）"
git fetch origin main
git reset --hard origin/main # 运行时三文件已 gitignore，不受影响

echo "==> [2/6] 同步后端依赖"
uv sync --frozen

echo "==> [3/6] 解包前端 dist"
rm -rf web/dist
tar -xzf dist.tgz -C web
rm -f dist.tgz

echo "==> [4/6] 优雅停止 agent（停止调度并等待当前决策轮结束，最长 300s）"
curl -sf -X POST "$API_BASE/api/agent/stop" >/dev/null 2>&1 || echo "   agent 未在运行或服务未启动，跳过"
for _ in $(seq 1 60); do
  in_round="$(curl -sf "$API_BASE/api/agent/live" 2>/dev/null \
    | uv run python -c "import sys,json; print(json.load(sys.stdin).get('in_round'))" 2>/dev/null || echo False)"
  [ "$in_round" = "False" ] && break
  sleep 5
done
[ "$in_round" != "False" ] && echo "   !! 等待决策轮结束超时，强制继续（持仓保留，仅重启进程）"

echo "==> [5/6] 重启服务 $SERVICE"
systemctl --user restart "$SERVICE"

echo "==> [6/6] 健康检查 $API_BASE/api/status"
ok=false
for _ in $(seq 1 12); do
  sleep 5
  if curl -sf "$API_BASE/api/status" >/dev/null 2>&1; then
    ok=true
    break
  fi
done

if [ "$ok" = true ]; then
  echo "==> 部署完成：$(git rev-parse --short HEAD)"
  exit 0
fi

echo "!! 健康检查失败，回滚到 $PREV_COMMIT"
git reset --hard "$PREV_COMMIT"
uv sync --frozen
systemctl --user restart "$SERVICE"
exit 1
