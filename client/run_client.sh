#!/bin/bash

# 摄像头客户端启动脚本

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 默认配置
CLIENT_ID=${CLIENT_ID:-"camera_001"}
SERVER_URL=${SERVER_URL:-"ws://localhost:8000/inspection/upload_stream"}
CAMERA_ID=${CAMERA_ID:-0}
FPS=${FPS:-30}
WIDTH=${WIDTH:-640}
HEIGHT=${HEIGHT:-480}
JPEG_QUALITY=${JPEG_QUALITY:-70}

# 打印横幅
print_banner() {
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}          摄像头采集客户端启动脚本${NC}"
    echo -e "${BLUE}============================================================${NC}"
}

# 打印使用说明
print_usage() {
    echo "使用方法:"
    echo "  $0 [命令] [选项]"
    echo ""
    echo "命令:"
    echo "  cli       - 启动命令行客户端"
    echo "  api       - 启动API服务"
    echo "  test      - 运行测试"
    echo "  help      - 显示帮助"
    echo ""
    echo "环境变量:"
    echo "  CLIENT_ID      - 客户端ID (默认: camera_001)"
    echo "  SERVER_URL     - 服务器地址 (默认: ws://localhost:8000/...)"
    echo "  CAMERA_ID      - 摄像头ID (默认: 0)"
    echo "  FPS            - 帧率 (默认: 30)"
    echo "  WIDTH          - 宽度 (默认: 640)"
    echo "  HEIGHT         - 高度 (默认: 480)"
    echo "  JPEG_QUALITY   - JPEG质量 (默认: 70)"
    echo ""
    echo "示例:"
    echo "  # 启动命令行客户端"
    echo "  $0 cli"
    echo ""
    echo "  # 启动API服务"
    echo "  $0 api"
    echo ""
    echo "  # 使用自定义配置"
    echo "  CLIENT_ID=my_camera FPS=60 $0 cli"
    echo ""
    echo "  # 运行测试"
    echo "  $0 test"
}

# 检查Python
check_python() {
    if ! command -v python3 &> /dev/null; then
        echo -e "${RED}❌ 未找到Python3${NC}"
        exit 1
    fi
    echo -e "${GREEN}✅ Python3已安装${NC}"
}

# 检查依赖
check_dependencies() {
    echo -e "${BLUE}📦 检查依赖...${NC}"
    
    if ! python3 -c "import cv2" 2>/dev/null; then
        echo -e "${YELLOW}⚠️  未安装opencv-python${NC}"
        echo -e "${BLUE}正在安装依赖...${NC}"
        pip3 install -r requirements.txt
    fi
    
    echo -e "${GREEN}✅ 依赖检查完成${NC}"
}

# 检查服务器
check_server() {
    echo -e "${BLUE}🔍 检查服务器连接...${NC}"
    
    if curl -s "http://localhost:8000/" > /dev/null 2>&1; then
        echo -e "${GREEN}✅ 服务器正在运行${NC}"
    else
        echo -e "${YELLOW}⚠️  服务器未运行${NC}"
        echo -e "${YELLOW}   请先启动服务器: uvicorn app.main:app --reload${NC}"
        read -p "是否继续? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
}

# 启动命令行客户端
start_cli() {
    print_banner
    check_python
    check_dependencies
    check_server
    
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}🚀 启动命令行客户端${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo -e "Client ID:     ${GREEN}${CLIENT_ID}${NC}"
    echo -e "服务器:        ${GREEN}${SERVER_URL}${NC}"
    echo -e "摄像头ID:      ${GREEN}${CAMERA_ID}${NC}"
    echo -e "帧率:          ${GREEN}${FPS}${NC}"
    echo -e "分辨率:        ${GREEN}${WIDTH}x${HEIGHT}${NC}"
    echo -e "JPEG质量:      ${GREEN}${JPEG_QUALITY}${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo ""
    
    python3 camera_client.py \
        --client-id "${CLIENT_ID}" \
        --server-url "${SERVER_URL}" \
        --camera-id ${CAMERA_ID} \
        --fps ${FPS} \
        --width ${WIDTH} \
        --height ${HEIGHT} \
        --jpeg-quality ${JPEG_QUALITY}
}

# 启动API服务
start_api() {
    print_banner
    check_python
    check_dependencies
    
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}🌐 启动API服务${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo -e "地址:          ${GREEN}http://0.0.0.0:8001${NC}"
    echo -e "API文档:       ${GREEN}http://localhost:8001/docs${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo ""
    
    python3 camera_client_api.py "$@"
}

# 运行测试
run_test() {
    print_banner
    check_python
    check_dependencies
    check_server
    
    echo ""
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}🧪 运行测试${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo ""
    
    python3 test_camera_client.py "$@"
}

# 主逻辑
main() {
    case "$1" in
        cli)
            shift
            start_cli "$@"
            ;;
        api)
            shift
            start_api "$@"
            ;;
        test)
            shift
            run_test "$@"
            ;;
        help|--help|-h)
            print_banner
            echo ""
            print_usage
            ;;
        *)
            print_banner
            echo ""
            echo -e "${YELLOW}请指定命令: cli, api, test, help${NC}"
            echo ""
            print_usage
            exit 1
            ;;
    esac
}

# 运行主函数
main "$@"
