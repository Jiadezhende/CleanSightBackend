# nginx-rtmp 快速安装指南

## 概述

本目录包含为 CleanSight 项目配置的最简 nginx-rtmp 中转服务，用于接收摄像头推流并提供给后端AI推理服务。

## 特性

✅ **纯中转服务** - 只转发流，不存储视频  
✅ **自动安装** - 提供一键部署脚本  
✅ **性能优化** - 针对实时推理场景优化  
✅ **监控支持** - 提供统计页面和监控脚本  
✅ **Python集成** - 完整的API和服务集成  

## 快速开始

### 1. 简化安装（推荐）

```bash
# 进入nginx-rtmp目录
cd nginx-rtmp

# 给脚本添加执行权限
chmod +x simple_install.sh

# 运行简化安装脚本
sudo ./simple_install.sh
```

### 2. 完整安装（如果需要）

```bash
# 使用完整安装脚本
chmod +x install_nginx_rtmp.sh
sudo ./install_nginx_rtmp.sh
```

### 3. 验证安装

```bash
# 检查nginx-rtmp服务状态
sudo systemctl status nginx-rtmp

# 检查RTMP模块
sudo /usr/local/nginx/sbin/nginx -V | grep rtmp

# 检查端口监听
ss -tlnp | grep -E '(80|1935)'

# 测试HTTP服务
curl http://localhost/health
```

### 3. 项目集成

```python
# 在Python代码中使用
from app.services.rtmp_service import rtmp_service

# 启动流处理
rtmp_service.start_stream_processor('camera01')

# 或通过API
import requests
requests.post('http://localhost:8000/rtmp/streams/camera01/start')
```

## 文件说明

- `nginx.conf` - nginx-rtmp配置文件
- `install_nginx_rtmp.sh` - 详细安装脚本
- `quick_deploy.sh` - 一键部署脚本
- `nginx_rtmp_manager.py` - Python管理工具
- `rtmp_stream_processor.py` - 流处理示例
- `test_rtmp_stream.py` - 推流测试工具
- `README.md` - 详细文档

## 使用流程

```
摄像头 → RTMP推流 → nginx-rtmp服务 → Python后端拉流 → AI推理
```

## 服务地址

部署后提供以下服务：

- RTMP推流: `rtmp://服务器IP:1935/live/{流名称}`
- 统计页面: `http://服务器IP/stat`
- 健康检查: `http://服务器IP/health`

## 故障排除

如遇问题，请查看：
- [详细文档](README.md)
- [部署指南](../RTMP_DEPLOYMENT_GUIDE.md)
- 运行监控: `sudo /usr/local/bin/rtmp-monitor`

## 环境要求

- Ubuntu 20.04+ / CentOS 7+
- 至少2GB内存
- Root权限
- 开放1935和80端口