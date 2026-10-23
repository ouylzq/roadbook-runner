#!/bin/sh
# 一键起看板：校验环境 → 起服务（默认 :8900）。用法：./serve.sh [--port 8901]
# 启动后的探活约束见 CLAUDE.md「常驻服务启动约束」：要在**下一次独立调用**里查
# lsof -iTCP:8900 和 /api/skills，不能只信本脚本打印的东西。
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "✗ 缺 .env：先 cp .env.example .env，填好 LLM 三行（QA_AGENT_BASE_URL / API_KEY / MODEL）。"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "没有 venv，创建并装依赖（含 Playwright Chromium，首次要下几分钟）…"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
  .venv/bin/playwright install chromium
fi

REPO="$(grep -E '^ROADBOOK_REPO=' .env | head -1 | cut -d= -f2- | tr -d ' ')"
REPO="${REPO:-../roadbook}"
if [ ! -f "$REPO/SKILL.md" ]; then
  echo "✗ ROADBOOK_REPO 指不到技能根：$REPO/SKILL.md 不存在。.env 里的 ROADBOOK_REPO 要指向 roadbook 仓库（场景来源）。"
  exit 1
fi

echo "自检：.venv/bin/python run.py check"
.venv/bin/python run.py check || echo "（自检有 ✗ 也能起服务，但跑批大概率会失败）"

if [ -d storage ]; then
  echo "数据目录：$(pwd)/storage（$(ls -1 storage 2>/dev/null | wc -l | tr -d ' ') 个历史 run，备份：./backup_data.sh）"
else
  echo "数据目录：$(pwd)/storage（空，还没有历史 run）"
fi

echo "起看板 http://127.0.0.1:8900 …"
exec .venv/bin/python run.py serve "$@"
