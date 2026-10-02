#!/usr/bin/env bash
# 一条命令：初始化数据库与演示数据（含孵化箱中途停机样例）并启动服务。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
DB="hatchery.db"

if [ ! -s "$DB" ]; then
  echo ">> 首次启动：建库并写入演示数据（含停机处置样例）"
  python3 -u -m hatchery.app --db "$DB" --port "$PORT" --init-seed
else
  echo ">> 数据库已存在，直接启动（如需重置：rm -f hatchery.db hatchery.db-wal hatchery.db-shm && rm -rf uploads）"
  python3 -u -m hatchery.app --db "$DB" --port "$PORT"
fi
