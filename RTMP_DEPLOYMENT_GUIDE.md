# CleanSight RTMP 服务部署指南

本文档介绍如何在云服务器上部署和使用 CleanSight 项目的 nginx-rtmp 中转服务。

## 快速部署

### 1. 上传项目文件

将项目上传到云服务器（如通过git clone或scp）：

```bash
# 方式1: 通过Git克隆
git clone https://github.com/your-repo/CleanSightBackend.git
cd CleanSightBackend

# 方式2: 上传压缩包并解压
scp cleansight.tar.gz user@server:/path/to/
ssh user@server "cd /path/to/ && tar -xzf cleansight.tar.gz"
```

### 2. 一键部署脚本

使用一键部署脚本快速安装：

```bash
cd nginx-rtmp
chmod +x quick_deploy.sh
sudo ./quick_deploy.sh
```

部署脚本将自动执行以下操作：
- 更新系统包
- 安装编译依赖
- 下载并编译nginx和nginx-rtmp模块
- 配置防火墙规则
- 创建监控脚本
- 启动服务

### 3. 验证部署

部署完成后，验证服务状态：

```bash
# 检查服务状态
sudo systemctl status nginx

# 检查端口监听
ss -tlnp | grep -E '(80|1935)'

# 运行监控脚本
sudo /usr/local/bin/rtmp-monitor

# 访问统计页面
curl http://localhost/stat
```

## 详细配置

### 环境变量配置

在 `.env` 文件中配置RTMP服务参数：

```env
# RTMP 服务器配置
CLEANSIGHT_RTMP_SERVER_HOST=localhost
CLEANSIGHT_RTMP_SERVER_PORT=1935
CLEANSIGHT_RTMP_BASE_URL=rtmp://localhost:1935/live
```

### 服务地址

部署完成后，服务提供以下地址：

- **RTMP推流**: `rtmp://服务器IP:1935/live/{流名称}`
- **RTMP拉流**: `rtmp://服务器IP:1935/live/{流名称}`
- **统计页面**: `http://服务器IP/stat`
- **健康检查**: `http://服务器IP/health`
- **HLS播放**: `http://服务器IP/hls/{流名称}.m3u8`

## 使用方法

### 1. 摄像头推流

#### 使用FFmpeg推流

```bash
# 从文件推流
ffmpeg -re -i video.mp4 -c copy -f flv rtmp://服务器IP:1935/live/camera01

# 从USB摄像头推流（Linux）
ffmpeg -f v4l2 -i /dev/video0 -c:v libx264 -preset ultrafast -f flv rtmp://服务器IP:1935/live/camera01

# 从IP摄像头推流
ffmpeg -i rtsp://camera_ip/stream -c:v libx264 -preset ultrafast -f flv rtmp://服务器IP:1935/live/camera01
```

#### 使用OBS Studio推流

1. 打开OBS Studio
2. 设置 -> 推流
3. 服务：自定义
4. 服务器：`rtmp://服务器IP:1935/live`
5. 推流密钥：`camera01`（或其他流名称）

### 2. Python后端拉流

在Python后端中使用OpenCV拉取RTMP流：

```python
import cv2

# 连接RTMP流
rtmp_url = "rtmp://localhost:1935/live/camera01"
cap = cv2.VideoCapture(rtmp_url)

if cap.isOpened():
    while True:
        ret, frame = cap.read()
        if ret:
            # 进行AI推理处理
            # 例如：YOLO目标检测、图像分类等
            process_frame(frame)
        else:
            print("读取帧失败")
            break

cap.release()
```

### 3. 使用项目API

项目提供了完整的RTMP管理API：

```python
import requests

base_url = "http://localhost:8000"

# 获取RTMP服务状态
response = requests.get(f"{base_url}/rtmp/status")
print(response.json())

# 获取流URL
response = requests.get(f"{base_url}/rtmp/streams/camera01/url")
print(response.json())

# 启动流处理器
response = requests.post(f"{base_url}/rtmp/streams/camera01/start")
print(response.json())

# 获取流统计信息
response = requests.get(f"{base_url}/rtmp/streams/camera01/stats")
print(response.json())
```

## 测试工具

### 1. 推流测试

使用项目提供的测试工具：

```bash
cd nginx-rtmp

# 生成测试视频
python3 test_rtmp_stream.py --generate --duration 60

# 从测试视频推流
python3 test_rtmp_stream.py --file test_video.mp4 --stream camera01

# 从摄像头推流
python3 test_rtmp_stream.py --camera 0 --stream camera01 --duration 30
```

### 2. 拉流测试

```bash
# 使用项目的流处理器测试
python3 rtmp_stream_processor.py

# 使用ffplay播放
ffplay rtmp://localhost:1935/live/camera01

# 使用VLC播放器
vlc rtmp://localhost:1935/live/camera01
```

## 性能优化

### 1. 系统级优化

```bash
# 调整系统参数
echo 'net.core.rmem_max = 134217728' >> /etc/sysctl.conf
echo 'net.core.wmem_max = 134217728' >> /etc/sysctl.conf
sysctl -p

# 增加文件描述符限制
echo '* soft nofile 65536' >> /etc/security/limits.conf
echo '* hard nofile 65536' >> /etc/security/limits.conf
```

### 2. Nginx配置优化

修改 `/etc/nginx/nginx.conf`：

```nginx
# 根据CPU核心数调整
worker_processes auto;

# 增加连接数
events {
    worker_connections 4096;
    use epoll;
}

# RTMP优化
rtmp {
    server {
        application live {
            # 减少延迟
            sync 100ms;
            
            # 调整缓冲区
            play_time_fix on;
            publish_time_fix on;
            
            # 限制连接数
            max_connections 100;
        }
    }
}
```

## 监控和维护

### 1. 服务监控

```bash
# 查看服务状态
sudo systemctl status nginx

# 查看实时日志
sudo tail -f /var/log/nginx/error.log

# 查看流统计
curl -s http://localhost/stat | grep -E "(publisher|subscriber)"

# 运行监控脚本
sudo /usr/local/bin/rtmp-monitor
```

### 2. 定期维护

```bash
# 清理临时HLS文件（建议每天执行）
find /tmp/hls -name "*.ts" -mtime +1 -delete
find /tmp/hls -name "*.m3u8" -mtime +1 -delete

# 清理旧日志
sudo logrotate -f /etc/logrotate.d/nginx

# 重载配置
sudo nginx -s reload
```

## 故障排除

### 常见问题

1. **推流连接被拒绝**
   - 检查防火墙设置
   - 确认nginx服务运行状态
   - 验证RTMP端口监听

2. **推流成功但拉流失败**
   - 检查流名称是否正确
   - 确认推流是否持续
   - 查看nginx错误日志

3. **高延迟问题**
   - 调整缓冲区设置
   - 优化网络配置
   - 使用更快的编码预设

4. **内存使用过高**
   - 限制HLS分片数量
   - 定期清理临时文件
   - 调整worker进程数

### 调试命令

```bash
# 检查端口占用
sudo lsof -i :1935
sudo lsof -i :80

# 测试网络连通性
telnet 服务器IP 1935

# 查看进程状态
ps aux | grep nginx

# 检查配置语法
sudo nginx -t

# 重启服务
sudo systemctl restart nginx
```

## 安全建议

1. **访问控制**
   - 配置IP白名单
   - 使用防火墙限制访问
   - 设置推流认证

2. **SSL/TLS加密**
   - 配置HTTPS访问统计页面
   - 使用RTMPS加密推流

3. **监控告警**
   - 设置异常流量监控
   - 配置服务状态告警
   - 定期安全检查

## 扩展功能

### 1. 录制功能（可选）

如需开启录制功能，修改nginx配置：

```nginx
application live {
    live on;
    record all;
    record_path /var/recordings;
    record_suffix .flv;
}
```

### 2. 推流认证

添加推流密钥验证：

```nginx
application live {
    live on;
    
    # 推流认证
    on_publish http://localhost:8000/rtmp/auth/publish;
    on_done http://localhost:8000/rtmp/auth/done;
}
```

### 3. 多路转发

配置流转发到多个目标：

```nginx
application live {
    live on;
    
    # 转发到其他RTMP服务器
    push rtmp://backup-server:1935/live;
}
```

通过以上配置，您可以在云服务器上成功部署和运行 CleanSight 的 RTMP 中转服务，实现摄像头到后端AI推理服务的流畅视频传输。