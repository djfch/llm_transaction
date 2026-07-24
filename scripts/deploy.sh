#!/usr/bin/env bash
# CD 部署脚本：GitHub Actions 以 stdin 发送同提交脚本；也可在服务器仓库内手动执行。
# 流程：固定提交 → 同步依赖 → 暂存 dist → 优雅停 agent → 切换前端 → 重启并检查。
# 任一步失败都会尝试恢复上一提交、Python 依赖和 web/dist（见 docs/DEPLOYMENT.md）。
set -Eeuo pipefail

REPO_ROOT="${DEPLOY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$REPO_ROOT"

SERVICE="${DEPLOY_SERVICE:-llm-transaction}"
API_BASE="${DEPLOY_API_BASE:-http://127.0.0.1:17577}"
TARGET_COMMIT="${DEPLOY_COMMIT:-}"
PREV_COMMIT="$(git rev-parse HEAD)"
ROLLBACK_DIR=""
ROLLBACK_ARMED=false
DIST_SWITCH_STARTED=false
SERVICE_TOUCHED=false

rollback() {
  local exit_code="${1:-1}"
  trap - ERR INT TERM
  set +e
  if [ "$ROLLBACK_ARMED" != true ]; then
    exit "$exit_code"
  fi
  echo "!! 部署失败，回滚到 $PREV_COMMIT"
  git reset --hard "$PREV_COMMIT"
  uv sync --frozen
  if [ -d "$ROLLBACK_DIR/previous-dist" ]; then
    rm -rf web/dist
    mv "$ROLLBACK_DIR/previous-dist" web/dist
  elif [ "$DIST_SWITCH_STARTED" = true ]; then
    rm -rf web/dist
  fi
  if [ "$SERVICE_TOUCHED" = true ]; then
    systemctl --user restart "$SERVICE"
  fi
  rm -rf "$ROLLBACK_DIR"
  exit "$exit_code"
}

on_error() {
  local exit_code=$?
  rollback "$exit_code"
}

wait_for_round() {
  local in_round="False"
  curl --connect-timeout 2 --max-time 5 -sf -X POST "$API_BASE/api/agent/stop" \
    >/dev/null 2>&1 \
    || echo "   agent 未在运行或服务未启动，跳过"
  for _ in $(seq 1 60); do
    in_round="$(curl --connect-timeout 2 --max-time 3 -sf "$API_BASE/api/agent/live" \
      2>/dev/null \
      | uv run python -c "import sys,json; print(json.load(sys.stdin).get('in_round'))" \
        2>/dev/null || echo False)"
    [ "$in_round" = "False" ] && return
    sleep 2
  done
  echo "   !! 等待决策轮结束超时，强制继续（持仓保留，仅重启进程）"
}

health_check() {
  for _ in $(seq 1 12); do
    sleep 5
    curl --connect-timeout 2 --max-time 3 -sf "$API_BASE/api/status" \
      >/dev/null 2>&1 && return 0
  done
  return 1
}

trap on_error ERR
trap 'rollback 130' INT TERM

echo "==> [1/6] 拉取 main 并固定部署提交（当前 $PREV_COMMIT）"
git fetch origin main
if [ -z "$TARGET_COMMIT" ]; then
  TARGET_COMMIT="$(git rev-parse origin/main)"
fi
if ! [[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "错误：DEPLOY_COMMIT 必须是 40 位 Git commit SHA" >&2
  exit 2
fi
if ! git cat-file -e "$TARGET_COMMIT^{commit}" 2>/dev/null; then
  echo "错误：部署提交不存在于服务器仓库：$TARGET_COMMIT" >&2
  exit 2
fi

ROLLBACK_DIR="$(mktemp -d "$PWD/.deploy-rollback.XXXXXX")"
ROLLBACK_ARMED=true
git reset --hard "$TARGET_COMMIT" # 运行时三文件已 gitignore，不受影响

echo "==> [2/6] 同步后端依赖"
uv sync --frozen

echo "==> [3/6] 解包并校验前端 dist"
mkdir -p "$ROLLBACK_DIR/staged"
tar -xzf dist.tgz -C "$ROLLBACK_DIR/staged"
if [ ! -f "$ROLLBACK_DIR/staged/dist/index.html" ]; then
  echo "错误：dist.tgz 缺少 dist/index.html" >&2
  rollback 2
fi

echo "==> [4/6] 优雅停止 agent（等待当前决策轮结束，最长约 300s）"
SERVICE_TOUCHED=true
wait_for_round
if [ -d web/dist ]; then
  mv web/dist "$ROLLBACK_DIR/previous-dist"
fi
DIST_SWITCH_STARTED=true
mv "$ROLLBACK_DIR/staged/dist" web/dist

echo "==> [5/6] 重启服务 $SERVICE"
systemctl --user restart "$SERVICE"

echo "==> [6/6] 健康检查 $API_BASE/api/status"
if ! health_check; then
  rollback 1
fi

ROLLBACK_ARMED=false
trap - ERR INT TERM
rm -rf "$ROLLBACK_DIR" || echo "   !! 无法清理回滚临时目录：$ROLLBACK_DIR"
rm -f dist.tgz || echo "   !! 无法清理 dist.tgz"
echo "==> 部署完成：$(git rev-parse --short HEAD)"
