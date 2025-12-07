# Nginx RTMP 中转服务

这个目录包含用于设置最简RTMP中转服务的配置文件和安装脚本。

## 功能特性

- **纯中转服务**: 只接收和转发RTMP流，不存储视频文件
- **高性能**: 优化的nginx配置，支持高并发流处理
- **统计监控**: 提供实时流统计页面
- **HLS支持**: 自动生成HLS流用于Web播放（可选）
- **健康检查**: HTTP健康检查端点

## 安装方法

### 方法一：使用Python管理脚本（推荐）

```bash
# 安装nginx-rtmp服务
sudo python3 nginx_rtmp_manager.py install

# 查看服务状态
python3 nginx_rtmp_manager.py info

# 启动服务
sudo python3 nginx_rtmp_manager.py start

# 停止服务
sudo python3 nginx_rtmp_manager.py stop

# 重启服务
sudo python3 nginx_rtmp_manager.py restart

# 重载配置
sudo python3 nginx_rtmp_manager.py reload
```

### 方法二：使用Shell脚本

```bash
# 设置执行权限
chmod +x install_nginx_rtmp.sh

# 运行安装脚本
sudo ./install_nginx_rtmp.sh
```

## 服务配置

安装完成后，服务将监听以下端口：

- **1935端口**: RTMP流接收端口
- **80端口**: HTTP服务端口（统计页面、HLS流）

## 使用方法

### 推流地址

```
rtmp://服务器IP:1935/live/流名称
```

例如：
```
rtmp://192.168.1.100:1935/live/camera01
```

### 拉流地址

```
rtmp://服务器IP:1935/live/流名称
```

### Web访问

- 统计页面: `http://服务器IP/stat`
- 健康检查: `http://服务器IP/health`

## 后端集成

在Python后端中，使用opencv拉取RTMP流：

```python
import cv2

# 拉取RTMP流
rtmp_url = "rtmp://localhost:1935/live/camera01"
cap = cv2.VideoCapture(rtmp_url)

if cap.isOpened():
    ret, frame = cap.read()
    if ret:
        # 处理帧数据
        pass
```

## 配置文件说明

### nginx.conf

主要配置部分：

- `rtmp.server.application live`: RTMP流应用配置
- `http.server`: HTTP服务配置，包括统计页面和HLS服务
- 缓存和性能优化设置

### 主要配置项

- `live on`: 启用实时流
- `record off`: 禁用录制（不存储）
- `wait_key on`: 等待关键帧
- `drop_idle_publisher 30s`: 30秒后断开不活跃的推流

## 性能优化

- 使用epoll事件模型
- 优化worker进程数量
- 合理设置缓冲区大小
- 启用sendfile和tcp_nopush

## 监控和调试

### 查看服务状态

```bash
# 检查nginx进程
ps aux | grep nginx

# 检查端口监听
ss -tlnp | grep -E '(80|1935)'

# 查看nginx日志
sudo tail -f /var/log/nginx/error.log
sudo tail -f /var/log/nginx/access.log
```

### 测试推流

使用FFmpeg测试推流：

```bash
# 测试推流（使用测试视频）
ffmpeg -re -i test.mp4 -c copy -f flv rtmp://localhost:1935/live/test

# 测试摄像头推流
ffmpeg -f v4l2 -i /dev/video0 -c:v libx264 -preset ultrafast -f flv rtmp://localhost:1935/live/camera
```

### 测试拉流

```bash
# 使用ffplay测试拉流
ffplay rtmp://localhost:1935/live/test

# 使用VLC播放器
vlc rtmp://localhost:1935/live/test
```

## 故障排除

### 常见问题

1. **端口被占用**
   ```bash
   sudo lsof -i :1935
   sudo lsof -i :80
   ```

2. **配置文件语法错误**
   ```bash
   sudo nginx -t
   ```

3. **权限问题**
   ```bash
   sudo chown -R www-data:www-data /var/cache/nginx
   sudo chown -R www-data:www-data /var/log/nginx
   ```

4. **防火墙阻止**
   ```bash
   sudo ufw allow 1935/tcp
   sudo ufw allow 80/tcp
   ```

### 日志位置

- 错误日志: `/var/log/nginx/error.log`
- 访问日志: `/var/log/nginx/access.log`
- 系统日志: `sudo journalctl -u nginx`

## 安全建议

1. 配置访问控制（IP白名单）
2. 使用SSL/TLS加密
3. 设置流密钥验证
4. 限制推流时长和大小
5. 监控异常流量

## 环境要求

- Ubuntu 18.04+ / Debian 9+ / CentOS 7+
- 至少2GB RAM
- 至少10GB磁盘空间（临时文件）
- 网络带宽根据流数量确定

## 相关命令

```bash
# 启动nginx
sudo systemctl start nginx

# 停止nginx
sudo systemctl stop nginx

# 重启nginx
sudo systemctl restart nginx

# 重载配置
sudo systemctl reload nginx

# 查看状态
sudo systemctl status nginx

# 开机自启
sudo systemctl enable nginx
```