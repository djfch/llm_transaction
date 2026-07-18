#!/usr/bin/env bash
# pre-commit 前端检查：ESLint + 类型检查 + 构建
set -e
cd "$(dirname "$0")/../web"
if [ ! -d node_modules ]; then
  npm ci
fi
npm run lint
npx tsc --noEmit
npm run build
