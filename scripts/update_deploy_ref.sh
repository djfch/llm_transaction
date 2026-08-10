#!/usr/bin/env bash
# 服务器部署与健康检查成功后，将 deploy 快进到同一完整提交。
set -Eeuo pipefail

TARGET_COMMIT="${DEPLOY_COMMIT:-}"
DEPLOY_REF="refs/heads/deploy"

if ! [[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "错误：DEPLOY_COMMIT 必须是 40 位 Git commit SHA" >&2
  exit 2
fi
git cat-file -e "$TARGET_COMMIT^{commit}" 2>/dev/null || {
  echo "错误：本地不存在部署提交 $TARGET_COMMIT" >&2
  exit 2
}

for attempt in $(seq 1 3); do
  remote_sha="$(git ls-remote --exit-code origin "$DEPLOY_REF" | awk '{print $1}')" || {
    echo "错误：远端 deploy 不存在，禁止自动猜测生产基线" >&2
    exit 1
  }
  if [ "$remote_sha" = "$TARGET_COMMIT" ]; then
    echo "==> deploy 已与生产提交对齐：$TARGET_COMMIT"
    exit 0
  fi

  git fetch --no-tags origin "$DEPLOY_REF"
  if ! git merge-base --is-ancestor "$remote_sha" "$TARGET_COMMIT"; then
    echo "错误：deploy=$remote_sha 无法快进到 $TARGET_COMMIT，禁止回退或改写生产指针" >&2
    exit 1
  fi

  echo "==> 更新 deploy（第 $attempt/3 次）：$remote_sha -> $TARGET_COMMIT"
  if git push origin "$TARGET_COMMIT:$DEPLOY_REF"; then
    confirmed="$(git ls-remote --exit-code origin "$DEPLOY_REF" | awk '{print $1}')"
    if [ "$confirmed" = "$TARGET_COMMIT" ]; then
      echo "==> deploy 已确认指向服务器生产提交：$TARGET_COMMIT"
      exit 0
    fi
  fi
  sleep 2
done

echo "错误：远端 deploy 与服务器实际 SHA 未对齐；请核对服务器 HEAD=$TARGET_COMMIT 后人工修复指针" >&2
exit 1