# CleanSight 部署指南

本文档介绍 CleanSight 后端系统的完整部署流程，包括硬件要求、环境配置、依赖安装和启动步骤。

## 目录

- [硬件要求](#硬件要求)
- [系统兼容性](#系统兼容性)
- [环境准备](#环境准备)
- [配置文件](#配置文件)
- [启动服务](#启动服务)
- [Docker 部署](#docker-部署)

---

## 硬件要求

### 推荐配置

- **GPU**: NVIDIA RTX 4090 或同级 CUDA 兼容 GPU
- **CPU**: Intel i7/i9 或 AMD Ryzen 7/9（8核以上）
- **内存**: 32GB RAM
- **存储**: 500GB SSD（用于视频段存储）

### 最低配置

- **CPU**: Intel i5 或 AMD Ryzen 5（4核以上）
- **内存**: 16GB RAM
- **存储**: 256GB SSD
- **GPU**: 可选（无 GPU 时自动降级到 CPU 模式）

**注意**: CPU 模式下推理速度较慢，建议仅用于开发测试。

---

## 系统兼容性

### 支持的操作系统

- ✅ **Windows 10/11** (x64)
- ✅ **Ubuntu 20.04+** (推荐 22.04 LTS)
- ✅ **macOS** 12+ (理论支持，未充分测试)

### Python 版本

- **要求**: Python 3.10+
- **推荐**: Python 3.11 或 3.12

---

## 环境准备

### 1. 安装 Python

#### Windows

```powershell
# 下载 Python 3.11 安装程序
# https://www.python.org/downloads/

# 验证安装
python --version
```

#### Ubuntu

```bash
sudo apt update
sudo apt install python3.11 python3.11-venv python3-pip
```

### 2. 安装 FFmpeg

FFmpeg 用于视频流解码，**必须安装**。

#### Windows

```powershell
# 使用 Chocolatey
choco install ffmpeg

# 或手动下载
# https://www.gyan.dev/ffmpeg/builds/
# 解压后添加到 PATH

# 验证安装
ffmpeg -version
```

#### Ubuntu

```bash
sudo apt install ffmpeg

# 验证安装
ffmpeg -version
```

### 3. 创建虚拟环境

```bash
# Windows
python -m venv .venv
.\.venv\Scripts\activate

# Linux/Mac
python3 -m venv .venv
source .venv/bin/activate
```

### 4. 安装依赖

```bash
# 安装所有依赖
pip install -r requirements.txt

# 如果使用国内镜像
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**关键依赖说明**:

- `torch==2.8.0` + `torchvision==0.23.0`: PyTorch (CUDA 支持)
- `ultralytics~=8.3.200`: YOLOv8 框架
- `opencv-python-headless==4.13.0`: OpenCV（无 GUI 版本，适合服务器）
- `fastapi==0.121.2` + `uvicorn==0.38.0`: Web 框架

**OpenCV 版本选择**:

- **生产环境**: `opencv-python-headless` (已在 requirements.txt 中)
- **开发环境**: 如需本地可视化，改用 `opencv-python`

### 5. 安装 MediaMTX

MediaMTX 用于 RTSP/RTMP 流中转。

#### Windows

1. 下载: [MediaMTX v1.15.4](https://github.com/bluenviron/mediamtx/releases)
2. 解压到 `mediamtx_v1.15.4/`
3. 运行: `mediamtx.exe`

#### Linux

```bash
# 下载并解压
wget https://github.com/bluenviron/mediamtx/releases/download/v1.15.5/mediamtx_v1.15.5_linux_amd64.tar.gz
tar -xzf mediamtx_v1.15.5_linux_amd64.tar.gz -C mediamtx_v1.15.5_linux_amd64

# 运行
cd mediamtx_v1.15.5_linux_amd64
./mediamtx
```

**端口配置**:
- RTMP: `1935`
- RTSP: `8004`
- HLS: `8888`
- WebRTC: `8889`

---

## 配置文件

### 1. 环境变量配置

根据部署环境选择配置文件：

- **开发环境**: `.env.dev`
- **测试环境**: `.env.test`
- **生产环境**: `.env`

#### 创建配置文件

参考 `.env.example` 创建对应的配置文件：

```bash
# 开发环境
cp .env.example .env.dev

# 生产环境
cp .env.example .env
```

#### 必需配置项

```ini
# 数据库配置
CLEANSIGHT_DB_HOST=localhost
CLEANSIGHT_DB_PORT=5432
CLEANSIGHT_DB_NAME=cleansight
CLEANSIGHT_DB_USER=cleansight
CLEANSIGHT_DB_PASSWORD=your_password

# 外部接口（必需）
CLEANSIGHT_FILE_PATH_INSERT_URL=http://your-server/api/file_path_insert
CLEANSIGHT_ALARM_REPORT_URL=http://your-server/api/alarm_report

# 应用配置
CLEANSIGHT_DEBUG=false
CLEANSIGHT_STRICT=1  # 生产环境严格模式
```

### 2. 推理配置

**文件**: `config/inference_config.yaml`

```yaml
stages:
  LEAK:
    models:
      - name: bubble_detection
        model_path: ./app/data/bubble-best.pt
        conf_threshold: 0.5

global:
  raw_fps: 30
  inference_fps: 20
  batch_size: 4
  ca_segment_len: 300
```

详细说明见 [配置指南](CONFIGURATION_GUIDE.md)。

### 3. 持久化配置

**文件**: `config/persistence_config.yaml`

```yaml
hls:
  workers: 2
  segment_duration: 10

alarm:
  workers: 1
  batch_interval: 30
```

### 4. 流处理配置

**文件**: `config/stream_config.yaml`

```yaml
decoder:
  default_fps: 30
  backpressure_ratio: 0.90

health_monitor:
  check_interval: 5.0
  heartbeat_timeout: 10.0
  max_restart_attempts: 5
```

---

## 启动服务

### 方式1: 使用启动脚本（推荐）

#### Windows

```powershell
# 开发环境
.\start_backend.ps1 dev

# 测试环境
.\start_backend.ps1 test

# 生产环境
.\start_backend.ps1 prod
```

#### Linux

```bash
# 开发环境
./start_backend.sh dev

# 测试环境
./start_backend.sh test

# 生产环境
./start_backend.sh prod
```

### 方式2: 手动启动

```bash
# 1. 激活虚拟环境
source .venv/bin/activate  # Linux/Mac
.\.venv\Scripts\activate   # Windows

# 2. 设置环境变量
export CLEANSIGHT_ENV=dev  # Linux/Mac
$env:CLEANSIGHT_ENV='dev'  # Windows

# 3. 启动 uvicorn
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 启动 MediaMTX

在另一个终端：

```bash
cd mediamtx_v1.15.4  # Windows
# 或 cd mediamtx_v1.15.5_linux_amd64  # Linux

./mediamtx  # Linux
# 或 .\mediamtx.exe  # Windows
```

### 验证部署

访问以下地址验证服务：

- **API 文档**: http://localhost:8000/docs
- **健康检查**: http://localhost:8000/ai/status

---

## Docker 部署

### 使用 Docker Compose

```bash
# 构建并启动
docker compose up --build

# 后台运行
docker compose up -d

# 停止服务
docker compose down
```

### 服务访问地址

- **API**: http://localhost:8000
- **PostgreSQL**: postgresql://cleansight:cleansight@localhost:5432/cleansight
- **RTMP**: rtmp://localhost:1935/live/<stream>
- **RTSP**: rtsp://localhost:8004/<path>
- **HLS**: http://localhost:8888/

### 查看日志

```bash
# 查看所有服务日志
docker compose logs -f

# 查看特定服务
docker compose logs -f app
docker compose logs -f mediamtx
```

---

## 生产环境优化

### 1. 性能调优

```yaml
# config/inference_config.yaml
global:
  inference_fps: 20        # 根据硬件调整
  batch_size: 8           # GPU内存充足时可增大
  visualization_threads: 8 # 多核CPU可增加
```

### 2. 日志配置

```bash
# 生产环境使用 INFO 级别
export LOG_LEVEL=INFO
```

### 3. 进程管理

使用 systemd 或 supervisor 管理服务进程。

#### systemd 示例

```ini
[Unit]
Description=CleanSight Backend
After=network.target

[Service]
Type=simple
User=cleansight
WorkingDirectory=/path/to/CleanSightBackend
Environment="CLEANSIGHT_ENV=prod"
ExecStart=/path/to/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## 故障排查

### 问题1: FFmpeg 未找到

**错误**: `FileNotFoundError: ffmpeg`

**解决**: 确保 FFmpeg 在 PATH 中，重新安装后重启终端

### 问题2: CUDA 不可用

**错误**: `CUDA not available`

**解决**:
1. 检查 NVIDIA 驱动: `nvidia-smi`
2. 验证 PyTorch CUDA 支持:
   ```python
   import torch
   print(torch.cuda.is_available())
   ```
3. 系统自动降级到 CPU 模式（性能较低）

### 问题3: 端口占用

**错误**: `Address already in use`

**解决**:
```bash
# 查找占用端口的进程
lsof -i :8000  # Linux/Mac
netstat -ano | findstr :8000  # Windows

# 终止进程或更改端口
```

---

## 相关文档

- [配置指南](CONFIGURATION_GUIDE.md) - 详细配置说明
- [快速开始](QUICK_START.md) - 快速开始指南
- [异常处理](EXCEPTION_HANDLING.md) - 容错机制

**最后更新**: 2026-01-30
