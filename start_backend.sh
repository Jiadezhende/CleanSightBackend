#!/bin/bash
# CleanSight Backend 启动脚本（Linux）
# 用法: ./start_backend.sh [dev|test|prod]
#
# 一条命令拉起整套环境：RTSP 网关（含 MediaMTX）+ 后端。
#
# 端口：**本脚本的 BASE_* 是 Linux 侧唯一声明处**（Windows 侧同理见 start_backend.ps1）。
#   改端口只改下面那五行，其余全部派生并注入给三方进程，无需动 .env*、mediamtx.yml、
#   config.ini、settings.py——它们里的端口只是「脱离本脚本单独跑某个进程」时的回退值。
#   dev / prod 用基准值（二者同端口，分属不同机器，不冲突）；test 整体 +2（与同机 prod 隔离）。

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

# ===== 端口（唯一声明处：改端口只改这五行）=====
# dev/prod 直接用基准值；test 整体 +2 以与同机 prod 隔离。
BASE_BACKEND=8000     # 后端 HTTP/WS
BASE_PROXY=8004       # 网关对外 RTSP（客户端连这个）
BASE_INTERNAL=18004   # MediaMTX RTSP（内部，网关回源）
BASE_RTP=8002         # MediaMTX RTP（UDP，内部）
BASE_RTCP=8003        # MediaMTX RTCP（UDP，内部）

case $ENV in
    test) OFFSET=2 ;;
    *)    OFFSET=0 ;;
esac

BACKEND_PORT=$((BASE_BACKEND + OFFSET))
PROXY_PORT=$((BASE_PROXY + OFFSET))
INTERNAL_PORT=$((BASE_INTERNAL + OFFSET))
RTP_PORT=$((BASE_RTP + OFFSET))
RTCP_PORT=$((BASE_RTCP + OFFSET))

# 注入给三方进程。这些 export 是环境变量，压得过 .env* 里的同名键
# （见 settings.py:_load_env_files 的 setdefault），故上面五行是运行时的唯一真源。
#   后端：uvicorn 绑定口 + 识别本机 MediaMTX 并回源改写（app/services/stream/manager.py:_rewrite_rtsp_url）
#   CLEANSIGHT_PORT 必须导出——否则 `python -m app.main` 路径（读 settings.port）会与本脚本分叉到不同端口
export CLEANSIGHT_PORT=$BACKEND_PORT
export CLEANSIGHT_MEDIAMTX_PROXY_PORT=$PROXY_PORT
export CLEANSIGHT_MEDIAMTX_INTERNAL_PORT=$INTERNAL_PORT
#   网关：对外监听端口 + 回源目标端口（mediamtx_gateway/main.py 认 GATEWAY_*，压过 config.ini）
export GATEWAY_LISTEN_PORT=$PROXY_PORT
export GATEWAY_TARGET_PORT=$INTERNAL_PORT
#   MediaMTX：覆盖 mediamtx.yml 中对应监听地址（MediaMTX 原生认 MTX_*）
export MTX_RTSPADDRESS=127.0.0.1:$INTERNAL_PORT
export MTX_RTPADDRESS=127.0.0.1:$RTP_PORT
export MTX_RTCPADDRESS=127.0.0.1:$RTCP_PORT

echo ""
echo "Ports ($ENV):"
echo "  backend HTTP/WS       : $BACKEND_PORT"
echo "  gateway RTSP (extern) : $PROXY_PORT"
echo "  MediaMTX RTSP (intern): $INTERNAL_PORT"
echo "  MediaMTX RTP/RTCP     : $RTP_PORT / $RTCP_PORT"
echo ""

# 确保日志目录存在（log-config 在应用代码前加载，需提前创建）
mkdir -p logs

# 后台启动 RTSP 网关（其自身拉起并守护 MediaMTX，MediaMTX 继承上面导出的 MTX_*）
python -m mediamtx_gateway.main &
GW_PID=$!
# 脚本退出（含 Ctrl+C）时一并清理网关及其子进程
trap 'kill $GW_PID 2>/dev/null' EXIT INT TERM

# reload 仅用于开发：prod/test 不挂文件监听，避免多 worker 与重载副作用
RELOAD=""
[ "$ENV" = "dev" ] && RELOAD="--reload"

# 前台启动后端
uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" $RELOAD --log-config logging_config.json
