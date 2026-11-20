#!/bin/bash
# 激活项目环境后运行后端；所有 daemon 由 app.main 的 lifespan 管理。
set -e
cd "$(dirname "$0")"
if [ "$#" -ne 0 ]; then
    echo "Usage: ./start_backend.sh" >&2
    exit 1
fi
if [ ! -f .venv/bin/activate ]; then
    echo "Missing .venv; prepare dependencies with /install" >&2
    exit 1
fi
source .venv/bin/activate
exec python -m app.main
