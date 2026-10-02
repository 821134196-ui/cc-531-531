#!/usr/bin/env bash
# 一条本地命令：建库（含孵化箱中途停机样例）并启动 Web 界面。
#   ./run.sh            首次自动初始化并启动
#   ./run.sh --reset    重置样例数据后启动
set -euo pipefail
cd "$(dirname "$0")"
PORT="${HATCH_PORT:-8080}"
if [[ ! -f data/hatchery.db ]] || [[ "${1:-}" == "--reset" ]]; then
  python3 -u -m app --reset --port "$PORT"
else
  python3 -u -m app --port "$PORT"
fi
