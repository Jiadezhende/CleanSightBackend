#!/bin/bash

# WebSocket 测试快捷脚本
# 用法: ./run_test.sh [test_type] [options]

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 默认配置
VIDEO_FILE="${VIDEO_FILE:-test_video.mp4}"
CLIENT_ID="${CLIENT_ID:-test_client_$(date +%s)}"
FPS="${FPS:-30}"
SERVER_URL="${SERVER_URL:-localhost:8000}"

# 打印帮助信息
print_help() {
    echo -e "${BLUE}WebSocket 测试脚本${NC}"
    echo ""
    echo "用法: $0 [命令] [选项]"
    echo ""
    echo "命令:"
    echo "  upload    - 测试视频上传接口"
    echo "  receive   - 测试视频接收接口"
    echo "  e2e       - 端到端完整测试 (推荐)"
    echo "  all       - 依次运行所有测试"
    echo "  help      - 显示此帮助信息"
    echo ""
    echo "选项:"
    echo "  --video FILE      视频文件路径 (默认: test_video.mp4)"
    echo "  --client-id ID    客户端ID (默认: 自动生成)"
    echo "  --fps N           帧率 (默认: 30)"
    echo "  --preview         显示预览窗口"
    echo "  --save            保存输出帧"
    echo "  --server URL      服务器地址 (默认: localhost:8000)"
    echo ""
    echo "环境变量:"
    echo "  VIDEO_FILE        默认视频文件"
    echo "  CLIENT_ID         默认客户端ID"
    echo "  FPS               默认帧率"
    echo "  SERVER_URL        默认服务器地址"
    echo ""
    echo "示例:"
    echo "  $0 e2e --preview"
    echo "  $0 upload --video my_video.mp4 --fps 15"
    echo "  $0 receive --client-id test_001"
    echo "  VIDEO_FILE=demo.mp4 FPS=60 $0 e2e --preview"
    echo ""
}

# 检查视频文件
check_video() {
    if [ ! -f "$VIDEO_FILE" ]; then
        echo -e "${RED}❌ 视频文件不存在: $VIDEO_FILE${NC}"
        echo -e "${YELLOW}💡 提示: 请确保视频文件存在，或使用 --video 参数指定其他文件${NC}"
        exit 1
    fi
    echo -e "${GREEN}✅ 视频文件: $VIDEO_FILE${NC}"
}

# 检查服务器
check_server() {
    echo -e "${BLUE}🔍 检查服务器连接...${NC}"
    if curl -s --max-time 2 "http://${SERVER_URL}/docs" > /dev/null 2>&1; then
        echo -e "${GREEN}✅ 服务器连接正常: http://${SERVER_URL}${NC}"
    else
        echo -e "${RED}❌ 无法连接到服务器: http://${SERVER_URL}${NC}"
        echo -e "${YELLOW}💡 提示: 请先启动 FastAPI 服务器:${NC}"
        echo -e "${YELLOW}   cd .. && uvicorn app.main:app --reload${NC}"
        exit 1
    fi
}

# 检查依赖
check_dependencies() {
    echo -e "${BLUE}🔍 检查依赖...${NC}"
    
    if ! python3 -c "import websockets" 2>/dev/null; then
        echo -e "${RED}❌ 缺少依赖: websockets${NC}"
        echo -e "${YELLOW}安装: pip install websockets${NC}"
        exit 1
    fi
    
    if ! python3 -c "import cv2" 2>/dev/null; then
        echo -e "${RED}❌ 缺少依赖: opencv-python${NC}"
        echo -e "${YELLOW}安装: pip install opencv-python${NC}"
        exit 1
    fi
    
    echo -e "${GREEN}✅ 所有依赖已安装${NC}"
}

# 运行上传测试
run_upload_test() {
    echo -e "${BLUE}📤 运行上传测试...${NC}"
    check_video
    check_server
    
    python3 test_websocket_upload.py \
        --video "$VIDEO_FILE" \
        --url "ws://${SERVER_URL}/inspection/upload_stream" \
        --client-id "$CLIENT_ID" \
        --fps "$FPS" \
        "$@"
}

# 运行接收测试
run_receive_test() {
    echo -e "${BLUE}📥 运行接收测试...${NC}"
    check_server
    
    python3 test_websocket_video.py \
        --url "ws://${SERVER_URL}/ai/video" \
        --client-id "$CLIENT_ID" \
        "$@"
}

# 运行端到端测试
run_e2e_test() {
    echo -e "${BLUE}🔄 运行端到端测试...${NC}"
    check_video
    check_server
    
    python3 test_websocket_e2e.py \
        --video "$VIDEO_FILE" \
        --upload-url "ws://${SERVER_URL}/inspection/upload_stream" \
        --receive-url "ws://${SERVER_URL}/ai/video" \
        --client-id "$CLIENT_ID" \
        --fps "$FPS" \
        "$@"
}

# 运行所有测试
run_all_tests() {
    echo -e "${BLUE}🧪 运行所有测试...${NC}"
    check_video
    check_server
    check_dependencies
    
    echo ""
    echo -e "${YELLOW}=== 测试 1/3: 上传测试 ===${NC}"
    run_upload_test --no-preview
    
    sleep 3
    
    echo ""
    echo -e "${YELLOW}=== 测试 2/3: 接收测试 ===${NC}"
    echo -e "${YELLOW}⚠️  注意: 需要先运行上传测试或确保有数据流${NC}"
    
    sleep 3
    
    echo ""
    echo -e "${YELLOW}=== 测试 3/3: 端到端测试 ===${NC}"
    run_e2e_test --no-preview
    
    echo ""
    echo -e "${GREEN}✅ 所有测试完成!${NC}"
}

# 解析参数
COMMAND=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        upload|receive|e2e|all|help)
            COMMAND="$1"
            shift
            ;;
        --video)
            VIDEO_FILE="$2"
            shift 2
            ;;
        --client-id)
            CLIENT_ID="$2"
            shift 2
            ;;
        --fps)
            FPS="$2"
            shift 2
            ;;
        --server)
            SERVER_URL="$2"
            shift 2
            ;;
        --preview|--save|--no-preview)
            EXTRA_ARGS+=("$1")
            shift
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

# 显示配置
show_config() {
    echo -e "${BLUE}⚙️  测试配置:${NC}"
    echo -e "   视频文件: ${GREEN}$VIDEO_FILE${NC}"
    echo -e "   客户端ID: ${GREEN}$CLIENT_ID${NC}"
    echo -e "   帧率: ${GREEN}$FPS${NC}"
    echo -e "   服务器: ${GREEN}$SERVER_URL${NC}"
    echo ""
}

# 主逻辑
main() {
    echo ""
    echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║       WebSocket 视频流测试脚本 - CleanSightBackend       ║${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    
    if [ -z "$COMMAND" ]; then
        print_help
        exit 0
    fi
    
    show_config
    check_dependencies
    
    case $COMMAND in
        upload)
            run_upload_test "${EXTRA_ARGS[@]}"
            ;;
        receive)
            run_receive_test "${EXTRA_ARGS[@]}"
            ;;
        e2e)
            run_e2e_test "${EXTRA_ARGS[@]}"
            ;;
        all)
            run_all_tests
            ;;
        help)
            print_help
            ;;
        *)
            echo -e "${RED}❌ 未知命令: $COMMAND${NC}"
            print_help
            exit 1
            ;;
    esac
}

main
