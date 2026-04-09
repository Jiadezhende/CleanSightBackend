#!/bin/bash
# CleanSight Backend 启动脚本（Linux）
# 用法: ./start_backend.sh [dev|test|prod]

ENV=${1:-dev}  # 默认开发环境

echo "Starting CleanSight Backend..."
echo ""

# 激活虚拟环境
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "Error: Virtual environment not found at .venv"
    echo "Please create it first: python3 -m venv .venv"
    exit 1
fi

# 根据参数设置环境
case $ENV in
    dev)
        export CLEANSIGHT_ENV='dev'
        echo "Environment: Development (.env.dev)"
        ;;
    test)
        export CLEANSIGHT_ENV='test'
        echo "Environment: Test (.env.test)"
        ;;
    prod)
        export CLEANSIGHT_ENV='prod'
        echo "Environment: Production (.env)"
        ;;
    *)
        echo "Error: Invalid environment '$ENV'"
        echo "Usage: ./start_backend.sh [dev|test|prod]"
        exit 1
        ;;
esac

echo ""

# 确保日志目录存在（log-config 在应用代码前加载，需提前创建）
mkdir -p logs

# 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --log-config logging_config.json
