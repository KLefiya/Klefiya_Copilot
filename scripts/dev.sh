#!/usr/bin/env bash
# 同时启动后端与前端开发服务器。Ctrl-C 一并停掉。
#
#   bash scripts/dev.sh
#
# 后端 http://127.0.0.1:8000   前端 http://localhost:5173
set -euo pipefail
cd "$(dirname "$0")/.."

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

python -m uvicorn backend.main:app --reload --port 8000 &
(cd frontend && npm run dev -- --port 5173) &
wait
