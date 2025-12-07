#!/bin/bash

# CleanSight Nginx RTMP 一键部署脚本
# 适用于 Ubuntu 20.04+ 云服务器快速部署

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 配置参数
RTMP_PORT=1935
HTTP_PORT=80
SERVER_IP=""

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查root权限
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "需要root权限运行此脚本"
        echo "请使用: sudo $0"
        exit 1
    fi
}

# 获取服务器IP
get_server_ip() {
    log_info "获取服务器IP地址..."
    
    # 尝试多种方式获取外网IP
    SERVER_IP=$(curl -s ifconfig.me) || \
    SERVER_IP=$(curl -s ipinfo.io/ip) || \
    SERVER_IP=$(wget -qO- ifconfig.me) || \
    SERVER_IP=$(hostname -I | awk '{print $1}')
    
    if [ -z "$SERVER_IP" ]; then
        log_warning "无法自动获取服务器IP，请手动输入"
        read -p "请输入服务器IP地址: " SERVER_IP
    fi
    
    log_info "服务器IP: $SERVER_IP"
}

# 更新系统
update_system() {
    log_info "更新系统包..."
    apt update -y
    apt upgrade -y
}

# 安装基础依赖
install_basic_deps() {
    log_info "安装基础依赖..."
    apt install -y \
        curl \
        wget \
        unzip \
        git \
        build-essential \
        software-properties-common \
        apt-transport-https \
        ca-certificates \
        gnupg \
        lsb-release
}

# 一键安装nginx-rtmp
quick_install_nginx_rtmp() {
    log_info "开始一键安装 nginx-rtmp..."
    
    # 检查是否已有nginx运行
    if systemctl is-active --quiet nginx 2>/dev/null; then
        log_warning "检测到nginx正在运行，将停止并备份配置"
        systemctl stop nginx
        if [ -f /etc/nginx/nginx.conf ]; then
            cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.backup.$(date +%Y%m%d_%H%M%S)
        fi
    fi
    
    # 下载并执行安装脚本
    INSTALL_SCRIPT="install_nginx_rtmp.sh"
    if [ -f "$INSTALL_SCRIPT" ]; then
        chmod +x "$INSTALL_SCRIPT"
        ./"$INSTALL_SCRIPT"
    else
        log_error "安装脚本不存在: $INSTALL_SCRIPT"
        return 1
    fi
}

# 优化配置
optimize_config() {
    log_info "优化nginx配置..."
    
    # 根据系统资源调整worker进程数
    CPU_CORES=$(nproc)
    sed -i "s/worker_processes auto/worker_processes $CPU_CORES/" /etc/nginx/nginx.conf
    
    # 增加连接数限制
    sed -i "s/worker_connections 1024/worker_connections 2048/" /etc/nginx/nginx.conf
    
    log_success "配置优化完成"
}

# 配置防火墙
setup_firewall() {
    log_info "配置防火墙规则..."
    
    # Ubuntu UFW
    if command -v ufw >/dev/null 2>&1; then
        ufw --force enable
        ufw allow ssh
        ufw allow $HTTP_PORT/tcp
        ufw allow $RTMP_PORT/tcp
        ufw reload
        log_success "UFW防火墙配置完成"
    
    # CentOS/RHEL firewalld
    elif command -v firewall-cmd >/dev/null 2>&1; then
        systemctl enable firewalld
        systemctl start firewalld
        firewall-cmd --permanent --add-service=ssh
        firewall-cmd --permanent --add-port=$HTTP_PORT/tcp
        firewall-cmd --permanent --add-port=$RTMP_PORT/tcp
        firewall-cmd --reload
        log_success "firewalld防火墙配置完成"
    
    else
        log_warning "未检测到防火墙管理工具，请手动配置"
        log_warning "需要开放端口: SSH(22), HTTP($HTTP_PORT), RTMP($RTMP_PORT)"
    fi
}

# 创建监控脚本
create_monitor_script() {
    log_info "创建服务监控脚本..."
    
    cat > /usr/local/bin/rtmp-monitor << 'EOF'
#!/bin/bash

# RTMP服务监控脚本
# 检查nginx进程和端口状态

check_nginx_process() {
    if pgrep nginx > /dev/null; then
        echo "✓ Nginx进程运行正常"
        return 0
    else
        echo "✗ Nginx进程未运行"
        return 1
    fi
}

check_rtmp_port() {
    if ss -tlnp | grep -q ":1935 "; then
        echo "✓ RTMP端口(1935)监听正常"
        return 0
    else
        echo "✗ RTMP端口(1935)未监听"
        return 1
    fi
}

check_http_port() {
    if ss -tlnp | grep -q ":80 "; then
        echo "✓ HTTP端口(80)监听正常"
        return 0
    else
        echo "✗ HTTP端口(80)未监听"
        return 1
    fi
}

check_disk_space() {
    usage=$(df /tmp | tail -1 | awk '{print $5}' | sed 's/%//')
    if [ $usage -lt 90 ]; then
        echo "✓ 磁盘空间充足 ($usage%)"
        return 0
    else
        echo "⚠ 磁盘空间不足 ($usage%)"
        return 1
    fi
}

show_stream_stats() {
    echo ""
    echo "=== 流统计信息 ==="
    if command -v curl >/dev/null 2>&1; then
        curl -s localhost/stat | grep -E "(publisher|subscriber)" | head -5 || echo "无法获取统计信息"
    else
        echo "curl未安装，无法获取统计信息"
    fi
}

main() {
    echo "RTMP服务监控报告 - $(date)"
    echo "================================"
    
    all_ok=true
    
    check_nginx_process || all_ok=false
    check_rtmp_port || all_ok=false  
    check_http_port || all_ok=false
    check_disk_space || all_ok=false
    
    show_stream_stats
    
    echo ""
    if $all_ok; then
        echo "✓ 所有检查通过，服务运行正常"
        exit 0
    else
        echo "✗ 发现问题，请检查服务状态"
        exit 1
    fi
}

main "$@"
EOF

    chmod +x /usr/local/bin/rtmp-monitor
    
    # 创建定时监控任务
    cat > /etc/cron.d/rtmp-monitor << EOF
# RTMP服务监控，每5分钟检查一次
*/5 * * * * root /usr/local/bin/rtmp-monitor > /var/log/rtmp-monitor.log 2>&1
EOF

    log_success "监控脚本创建完成"
}

# 性能测试
performance_test() {
    log_info "进行性能测试..."
    
    # 检查系统资源
    echo "系统资源:"
    echo "  CPU核心数: $(nproc)"
    echo "  内存总量: $(free -h | grep Mem | awk '{print $2}')"
    echo "  磁盘空间: $(df -h / | tail -1 | awk '{print $4}')"
    
    # 测试nginx配置
    if nginx -t; then
        log_success "Nginx配置语法正确"
    else
        log_error "Nginx配置语法错误"
        return 1
    fi
    
    # 测试端口连通性
    if ss -tlnp | grep -q ":$RTMP_PORT "; then
        log_success "RTMP端口监听正常"
    else
        log_error "RTMP端口未监听"
        return 1
    fi
    
    if ss -tlnp | grep -q ":$HTTP_PORT "; then
        log_success "HTTP端口监听正常"
    else
        log_error "HTTP端口未监听"
        return 1
    fi
}

# 显示部署结果
show_deployment_result() {
    echo ""
    echo "========================================"
    echo "  CleanSight RTMP 服务部署完成!"
    echo "========================================"
    echo ""
    echo "服务信息:"
    echo "  服务器IP: $SERVER_IP"
    echo "  RTMP推流: rtmp://$SERVER_IP:$RTMP_PORT/live/{stream_name}"
    echo "  RTMP拉流: rtmp://$SERVER_IP:$RTMP_PORT/live/{stream_name}"
    echo "  统计页面: http://$SERVER_IP/stat"
    echo "  健康检查: http://$SERVER_IP/health"
    echo ""
    echo "推流示例:"
    echo "  ffmpeg -re -i video.mp4 -c copy -f flv rtmp://$SERVER_IP:$RTMP_PORT/live/camera01"
    echo ""
    echo "拉流示例:"
    echo "  ffplay rtmp://$SERVER_IP:$RTMP_PORT/live/camera01"
    echo "  vlc rtmp://$SERVER_IP:$RTMP_PORT/live/camera01"
    echo ""
    echo "Python代码示例:"
    echo "  import cv2"
    echo "  cap = cv2.VideoCapture('rtmp://$SERVER_IP:$RTMP_PORT/live/camera01')"
    echo ""
    echo "管理命令:"
    echo "  sudo systemctl start|stop|restart|reload nginx"
    echo "  sudo /usr/local/bin/rtmp-monitor  # 服务监控"
    echo "  sudo tail -f /var/log/nginx/error.log  # 查看日志"
    echo ""
    echo "配置文件:"
    echo "  /etc/nginx/nginx.conf  # 主配置文件"
    echo "  /var/log/nginx/  # 日志目录"
    echo ""
    log_success "部署完成！您现在可以开始推流测试了"
}

# 主安装流程
main() {
    echo "CleanSight Nginx RTMP 一键部署脚本"
    echo "=================================="
    
    check_root
    get_server_ip
    
    log_info "开始部署 RTMP 服务..."
    
    update_system
    install_basic_deps
    quick_install_nginx_rtmp
    optimize_config
    setup_firewall
    create_monitor_script
    
    # 重启nginx服务
    systemctl restart nginx
    
    # 等待服务启动
    sleep 3
    
    performance_test
    show_deployment_result
    
    log_success "一键部署完成！"
}

# 错误处理
trap 'echo -e "${RED}部署过程中发生错误！${NC}"; exit 1' ERR

# 执行主函数
main "$@"