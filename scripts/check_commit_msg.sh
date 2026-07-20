#!/usr/bin/env bash
# commit-msg 钩子：校验提交信息首行格式（对齐仓库历史风格 "type: 中文描述"）。
# pre-commit commit-msg 阶段会把提交信息文件路径作为第一个参数传入。
set -u

msg_file="${1:?缺少提交信息文件参数}"
first_line=$(head -n1 "$msg_file")

# type 必填，scope 可选，描述非空且首行 ≤72 字
pattern='^(feat|fix|docs|style|refactor|perf|test|chore|ci|build|revert)(\([a-zA-Z0-9_/.-]+\))?!?: .{1,72}$'

if ! printf '%s' "$first_line" | grep -qE "$pattern"; then
  echo "✗ 提交信息首行格式不符：$first_line"
  echo "  要求：type: 描述（首行 ≤72 字）"
  echo "  type ∈ feat|fix|docs|style|refactor|perf|test|chore|ci|build|revert"
  echo "  示例：feat: 决策时间线卡片支持展开工具调用详情"
  exit 1
fi
