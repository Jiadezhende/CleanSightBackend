#!/bin/bash

# 简化版 nginx-rtmp 安装脚本
set -e

echo "=== 开始安装 nginx-rtmp 服务 ==="

# 检查是否为root
if [ "$EUID" -ne 0 ]; then
    echo "请使用 sudo 运行此脚本"
    exit 1
fi

# 停止现有nginx
echo "停止现有nginx服务..."
systemctl stop nginx 2>/dev/null || true
systemctl stop nginx-rtmp 2>/dev/null || true
pkill nginx 2>/dev/null || true

# 安装依赖
echo "安装编译依赖..."
apt update -qq
apt install -y build-essential libpcre3-dev libssl-dev zlib1g-dev wget unzip

# 创建临时目录
TEMP_DIR="/tmp/nginx-rtmp-$(date +%s)"
mkdir -p $TEMP_DIR
cd $TEMP_DIR

# 下载源码
echo "下载nginx和rtmp模块..."
wget -q http://nginx.org/download/nginx-1.20.2.tar.gz
wget -q https://github.com/arut/nginx-rtmp-module/archive/master.zip

# 解压
tar -xzf nginx-1.20.2.tar.gz
unzip -q master.zip

# 编译nginx
echo "编译nginx (需要几分钟)..."
cd nginx-1.20.2
./configure \
    --prefix=/usr/local/nginx \
    --sbin-path=/usr/local/nginx/sbin/nginx \
    --conf-path=/usr/local/nginx/conf/nginx.conf \
    --error-log-path=/var/log/nginx/error.log \
    --http-log-path=/var/log/nginx/access.log \
    --pid-path=/var/run/nginx.pid \
    --with-http_ssl_module \
    --with-http_realip_module \
    --with-http_stub_status_module \
    --add-module=../nginx-rtmp-module-master

make -j$(nproc)
make install

# 创建nginx用户
useradd --system --home /var/cache/nginx --shell /sbin/nologin nginx 2>/dev/null || true

# 创建目录
mkdir -p /var/log/nginx /var/www/html
chown -R nginx:nginx /var/log/nginx

# 创建systemd服务
cat > /etc/systemd/system/nginx-rtmp.service << 'EOF'
[Unit]
Description=NGINX HTTP and RTMP Server
After=network.target

[Service]
Type=forking
PIDFile=/var/run/nginx.pid
ExecStartPre=/usr/local/nginx/sbin/nginx -t
ExecStart=/usr/local/nginx/sbin/nginx
ExecReload=/bin/kill -s HUP $MAINPID
ExecStop=/bin/kill -s QUIT $MAINPID
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# 复制配置文件
if [ -f ~/CleanSightBackend/nginx-rtmp/nginx.conf ]; then
    cp ~/CleanSightBackend/nginx-rtmp/nginx.conf /usr/local/nginx/conf/nginx.conf
    echo "已使用项目配置文件"
else
    echo "项目配置文件未找到，请手动复制配置"
fi

# 创建软链接
ln -sf /usr/local/nginx/sbin/nginx /usr/local/bin/nginx

# 启动服务
systemctl daemon-reload
systemctl enable nginx-rtmp
systemctl start nginx-rtmp

# 清理临时文件
cd /
rm -rf $TEMP_DIR

# 检查安装结果
echo ""
echo "=== 安装完成 ==="
echo "检查RTMP模块: $(/usr/local/nginx/sbin/nginx -V 2>&1 | grep -o rtmp || echo '未找到')"
echo "服务状态: $(systemctl is-active nginx-rtmp 2>/dev/null || echo '未运行')"
echo "端口监听:"
ss -tlnp | grep -E '(80|1935)' || echo "  未监听RTMP端口"

echo ""
echo "验证命令:"
echo "  sudo systemctl status nginx-rtmp"
echo "  sudo /usr/local/nginx/sbin/nginx -V | grep rtmp"
echo "  ss -tlnp | grep -E '(80|1935)'"
echo "  curl http://localhost/health"