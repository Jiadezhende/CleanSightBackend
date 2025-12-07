#!/bin/bash

# Nginx RTMP 服务安装和配置脚本
# 适用于 Ubuntu/Debian 系统

set -e  # 遇到错误时退出

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 日志函数
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

# 检查是否为root用户
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "此脚本需要root权限运行"
        echo "请使用: sudo $0"
        exit 1
    fi
}

# 检测系统类型
detect_system() {
    log_info "检测系统类型..."
    
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$NAME
        VER=$VERSION_ID
    else
        log_error "无法检测系统类型"
        exit 1
    fi
    
    log_info "系统: $OS $VER"
}

# 更新系统包
update_system() {
    log_info "更新系统包列表..."
    apt update -y
    log_success "系统包列表更新完成"
}

# 安装依赖
install_dependencies() {
    log_info "安装编译依赖..."
    
    apt install -y \
        build-essential \
        libpcre3-dev \
        libssl-dev \
        zlib1g-dev \
        libxml2-dev \
        libxslt1-dev \
        libgd-dev \
        libgeoip-dev \
        wget \
        unzip \
        git
    
    log_success "依赖安装完成"
}

# 下载nginx和nginx-rtmp模块
download_nginx() {
    log_info "创建临时工作目录..."
    WORK_DIR="/tmp/nginx-rtmp-build"
    mkdir -p $WORK_DIR
    cd $WORK_DIR
    
    # nginx版本
    NGINX_VERSION="1.22.1"
    NGINX_RTMP_VERSION="1.2.2"
    
    log_info "下载 nginx $NGINX_VERSION..."
    wget -q "http://nginx.org/download/nginx-$NGINX_VERSION.tar.gz" -O nginx.tar.gz
    
    log_info "下载 nginx-rtmp-module $NGINX_RTMP_VERSION..."
    wget -q "https://github.com/arut/nginx-rtmp-module/archive/v$NGINX_RTMP_VERSION.tar.gz" -O nginx-rtmp.tar.gz
    
    log_info "解压文件..."
    tar -xzf nginx.tar.gz
    tar -xzf nginx-rtmp.tar.gz
    
    log_success "下载和解压完成"
}

# 编译nginx
compile_nginx() {
    log_info "开始编译nginx..."
    
    cd nginx-*
    
    # 配置编译选项
    ./configure \
        --prefix=/etc/nginx \
        --sbin-path=/usr/sbin/nginx \
        --modules-path=/usr/lib/nginx/modules \
        --conf-path=/etc/nginx/nginx.conf \
        --error-log-path=/var/log/nginx/error.log \
        --http-log-path=/var/log/nginx/access.log \
        --pid-path=/var/run/nginx.pid \
        --lock-path=/var/run/nginx.lock \
        --http-client-body-temp-path=/var/cache/nginx/client_temp \
        --http-proxy-temp-path=/var/cache/nginx/proxy_temp \
        --http-fastcgi-temp-path=/var/cache/nginx/fastcgi_temp \
        --http-uwsgi-temp-path=/var/cache/nginx/uwsgi_temp \
        --http-scgi-temp-path=/var/cache/nginx/scgi_temp \
        --with-http_ssl_module \
        --with-http_realip_module \
        --with-http_addition_module \
        --with-http_sub_module \
        --with-http_dav_module \
        --with-http_flv_module \
        --with-http_mp4_module \
        --with-http_gunzip_module \
        --with-http_gzip_static_module \
        --with-http_random_index_module \
        --with-http_secure_link_module \
        --with-http_stub_status_module \
        --with-http_auth_request_module \
        --with-http_xslt_module=dynamic \
        --with-http_image_filter_module=dynamic \
        --with-http_geoip_module=dynamic \
        --with-threads \
        --with-stream \
        --with-stream_ssl_module \
        --with-stream_ssl_preread_module \
        --with-stream_realip_module \
        --with-stream_geoip_module=dynamic \
        --with-http_slice_module \
        --with-file-aio \
        --with-http_v2_module \
        --add-module=../nginx-rtmp-module-*
    
    # 编译
    make -j$(nproc)
    
    log_success "nginx编译完成"
}

# 安装nginx
install_nginx() {
    log_info "安装nginx..."
    
    # 安装
    make install
    
    # 创建必要的目录
    mkdir -p /var/cache/nginx/client_temp
    mkdir -p /var/cache/nginx/proxy_temp
    mkdir -p /var/cache/nginx/fastcgi_temp
    mkdir -p /var/cache/nginx/uwsgi_temp
    mkdir -p /var/cache/nginx/scgi_temp
    mkdir -p /var/log/nginx
    mkdir -p /var/www/html
    
    # 设置权限
    chown -R www-data:www-data /var/cache/nginx
    chown -R www-data:www-data /var/log/nginx
    chown -R www-data:www-data /var/www/html
    
    log_success "nginx安装完成"
}

# 创建nginx用户
create_nginx_user() {
    log_info "创建nginx用户..."
    
    if ! id "www-data" &>/dev/null; then
        useradd --system --home /var/cache/nginx --shell /sbin/nologin --comment "nginx user" --user-group www-data
    fi
    
    log_success "nginx用户创建完成"
}

# 创建systemd服务
create_systemd_service() {
    log_info "创建systemd服务..."
    
    cat > /etc/systemd/system/nginx.service << 'EOF'
[Unit]
Description=The nginx HTTP and reverse proxy server
Documentation=http://nginx.org/en/docs/
After=network.target remote-fs.target nss-lookup.target

[Service]
Type=forking
PIDFile=/var/run/nginx.pid
ExecStartPre=/usr/sbin/nginx -t
ExecStart=/usr/sbin/nginx
ExecReload=/bin/kill -s HUP $MAINPID
KillSignal=SIGQUIT
TimeoutStopSec=5
KillMode=process
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
    
    # 重新加载systemd
    systemctl daemon-reload
    systemctl enable nginx
    
    log_success "systemd服务创建完成"
}

# 复制配置文件
copy_config() {
    log_info "设置配置文件..."
    
    # 备份原始配置
    if [ -f /etc/nginx/nginx.conf ]; then
        cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.backup
    fi
    
    # 检查项目配置文件是否存在
    PROJECT_CONFIG="$(dirname $(dirname $(readlink -f $0)))/nginx-rtmp/nginx.conf"
    if [ -f "$PROJECT_CONFIG" ]; then
        cp "$PROJECT_CONFIG" /etc/nginx/nginx.conf
        log_success "使用项目配置文件"
    else
        log_warning "项目配置文件未找到，使用默认配置"
        # 这里可以创建一个简单的默认配置
    fi
    
    # 创建统计页面样式表
    cat > /etc/nginx/stat.xsl << 'EOF'
<?xml version="1.0" encoding="utf-8" ?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
<xsl:template match="/">
    <html>
    <head>
        <title>RTMP Statistics</title>
    </head>
    <body>
        <h1>RTMP Statistics</h1>
        <xsl:apply-templates/>
    </body>
    </html>
</xsl:template>
</xsl:stylesheet>
EOF
    
    log_success "配置文件设置完成"
}

# 配置防火墙
configure_firewall() {
    log_info "配置防火墙..."
    
    if command -v ufw &> /dev/null; then
        ufw allow 1935/tcp comment 'RTMP'
        ufw allow 80/tcp comment 'HTTP'
        log_success "UFW防火墙规则已添加"
    elif command -v firewall-cmd &> /dev/null; then
        firewall-cmd --permanent --add-port=1935/tcp
        firewall-cmd --permanent --add-port=80/tcp
        firewall-cmd --reload
        log_success "firewalld防火墙规则已添加"
    else
        log_warning "未检测到防火墙工具，请手动开放1935端口(RTMP)和80端口(HTTP)"
    fi
}

# 测试配置
test_config() {
    log_info "测试nginx配置..."
    
    if /usr/sbin/nginx -t; then
        log_success "nginx配置文件语法正确"
    else
        log_error "nginx配置文件语法错误"
        exit 1
    fi
}

# 启动服务
start_service() {
    log_info "启动nginx服务..."
    
    systemctl start nginx
    
    if systemctl is-active --quiet nginx; then
        log_success "nginx服务启动成功"
    else
        log_error "nginx服务启动失败"
        exit 1
    fi
}

# 显示服务状态
show_status() {
    log_info "服务状态信息:"
    echo "----------------------------------------"
    systemctl status nginx --no-pager -l
    echo "----------------------------------------"
    
    log_info "RTMP服务地址:"
    echo "  推流地址: rtmp://$(hostname -I | awk '{print $1}'):1935/live"
    echo "  统计页面: http://$(hostname -I | awk '{print $1}')/stat"
    echo "  健康检查: http://$(hostname -I | awk '{print $1}')/health"
    
    log_info "常用命令:"
    echo "  启动服务: sudo systemctl start nginx"
    echo "  停止服务: sudo systemctl stop nginx"
    echo "  重启服务: sudo systemctl restart nginx"
    echo "  重载配置: sudo systemctl reload nginx"
    echo "  查看状态: sudo systemctl status nginx"
    echo "  查看日志: sudo journalctl -u nginx -f"
}

# 清理临时文件
cleanup() {
    log_info "清理临时文件..."
    rm -rf $WORK_DIR
    log_success "清理完成"
}

# 主函数
main() {
    log_info "开始安装 Nginx RTMP 服务..."
    
    check_root
    detect_system
    update_system
    install_dependencies
    create_nginx_user
    download_nginx
    compile_nginx
    install_nginx
    create_systemd_service
    copy_config
    configure_firewall
    test_config
    start_service
    show_status
    cleanup
    
    log_success "Nginx RTMP 服务安装完成!"
}

# 捕获错误和清理
trap cleanup EXIT

# 执行主函数
main "$@"