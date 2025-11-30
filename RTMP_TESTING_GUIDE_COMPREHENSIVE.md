# RTMP 测试完整指南

本指南涵盖 CleanSightBackend 项目的 RTMP 流媒体测试的完整流程,包括环境搭建、测试执行和问题排查。

---

## 📚 目录

1. [快速开始](#快速开始)
2. [环境准备](#环境准备)
3. [测试方案](#测试方案)
4. [问题排查](#问题排查)
5. [参考文档](#参考文档)

---

## 🚀 快速开始

### 最简单的测试流程

```powershell
# 1. 启动 MediaMTX (终端 1)
cd mediamtx_v1.15.4
.\mediamtx.exe

# 2. 启动后端 API (终端 2)
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload

# 3. 运行一键测试 (终端 3)
cd test
python test_rtmp_quick.py --duration 30
```

**预期结果**: 
- ✅ 推流成功
- ✅ 后端捕获 RTMP 流
- ✅ 接收 AI 推理结果
- ✅ 测试报告显示帧率达标

---

## 🔧 环境准备

### 1. MediaMTX 安装

MediaMTX 是一个轻量级的 RTMP/RTSP 服务器。

**详细安装步骤**: [MEDIAMTX_SETUP.md](./MEDIAMTX_SETUP.md)

**快速安装**:
```powershell
# 下载
# https://github.com/bluenviron/mediamtx/releases
# 下载 mediamtx_vX.X.X_windows_amd64.zip

# 解压到项目目录
# CleanSightBackend/mediamtx_vX.X.X/

# 启动
cd mediamtx_vX.X.X
.\mediamtx.exe
```

**验证**: 应该看到输出 `INF [RTMP] listener opened on :1935`

---

### 2. FFmpeg 安装

FFmpeg 用于 RTMP 推流测试。

**详细安装步骤**: [FFMPEG_INSTALL.md](./FFMPEG_INSTALL.md)

**快速安装 (Chocolatey)**:
```powershell
# 使用 Chocolatey (推荐)
choco install ffmpeg -y

# 验证
ffmpeg -version
```

**手动安装**:
1. 下载: https://www.gyan.dev/ffmpeg/builds/
2. 下载 `ffmpeg-release-essentials.zip`
3. 解压到项目目录 `ffmpeg/bin/`
4. 添加到 PATH

---

### 3. Python 环境

```powershell
# 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 安装依赖
pip install -r requirements.txt

# 验证关键包
python -c "import cv2; print(f'OpenCV {cv2.__version__}')"
python -c "import websockets; print('websockets installed')"
```

---

## 🧪 测试方案

### 方案对比

| 测试脚本 | 用途 | 推流方式 | 适用场景 |
|---------|------|----------|----------|
| **test_rtmp_quick.py** | ✅ 一键自动化测试 | 自动启动 ffmpeg | **推荐** - 日常快速验证 |
| **test_rtmp_integration.py** | 集成测试 | 依赖外部推流 | 调试 - 分步验证各环节 |

---

### 方案 A: 一键自动化测试 (推荐)

**特点**: 
- ✅ 自动启动 ffmpeg 推流
- ✅ 自动捕获和接收
- ✅ 自动清理资源
- ✅ 生成测试报告

**前置条件**:
- MediaMTX 运行中
- 后端 API 运行中
- ffmpeg 已安装

**使用方法**:
```powershell
cd test
python test_rtmp_quick.py --duration 30

# 自定义参数
python test_rtmp_quick.py \
    --client_id camera_001 \
    --duration 60 \
    --fps 30
```

**测试流程**:
```
1. 检查前置条件 (ffmpeg, 视频文件, 后端API)
   ↓
2. 启动 ffmpeg 推流 (subprocess)
   ↓
3. 等待 8 秒 (确保流稳定)
   ↓
4. 启动后端 RTMP 捕获
   ↓
5. 等待 5 秒 (后端连接初始化)
   ↓
6. 连接 WebSocket 接收推理结果
   ↓
7. 运行指定时长
   ↓
8. 停止捕获和推流
   ↓
9. 生成测试报告
```

**预期输出**:
```
============================================================
🚀 RTMP 快速测试
============================================================
🔍 检查前置条件...
✅ ffmpeg: C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe
✅ 测试视频: E:\...\test\test_video.mp4
✅ 后端 API: http://localhost:8000
✅ 前置条件检查完成

📤 启动 ffmpeg 推流到 rtmp://localhost:1935/live/test
⏳ 等待推流建立连接 (5秒)...
✅ ffmpeg 推流进程运行中

⏳ 等待推流建立...
   (观察 MediaMTX 日志,应该看到 'is publishing' 消息)

📥 启动后端 RTMP 捕获: quick_test
✅ RTMP 捕获已启动

⏳ 等待后端捕获初始化...

📺 连接 WebSocket: ws://localhost:8000/ai/video?client_id=quick_test
✅ WebSocket 已连接，开始接收推理结果...

⏱️  已运行 10s | 已接收 300 帧 | 平均 30.0 FPS
⏱️  已运行 20s | 已接收 600 帧 | 平均 30.0 FPS
⏱️  已运行 30s | 已接收 900 帧 | 平均 30.0 FPS

🛑 停止后端 RTMP 捕获: quick_test
✅ RTMP 捕获已停止

🛑 停止 ffmpeg 推流
✅ ffmpeg 推流已正常停止

============================================================
📊 测试报告
============================================================
测试时长: 30.3 秒
接收帧数: 900 帧
平均帧率: 29.7 FPS
目标帧率: 30 FPS
✅ 测试通过 (帧率达标)
============================================================
```

---

### 方案 B: 手动分步测试 (调试用)

**特点**:
- 🔍 每个环节独立运行
- 🐛 便于定位问题
- 📊 详细的状态输出

**使用方法**:

**终端 1: MediaMTX**
```powershell
cd mediamtx_v1.15.4
.\mediamtx.exe
```

**终端 2: 后端 API**
```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

**终端 3: 手动推流 (保持运行)**
```powershell
cd test
ffmpeg -re -stream_loop -1 -i test_video.mp4 -c:v libx264 -preset ultrafast -tune zerolatency -f flv rtmp://localhost:1935/live/test
```

**观察 MediaMTX 日志**,确认看到:
```
INF [RTMP] [conn [::1]:xxxxx] opened
INF [RTMP] [conn [::1]:xxxxx] is publishing to path 'live/test', 2 tracks (H264, MPEG-1/2 Audio)
```

**终端 4: 运行测试**
```powershell
cd test
python test_rtmp_integration.py --client_id camera_001 --rtmp_url rtmp://localhost:1935/live/test --duration 30
```

**优点**:
- 可以看到 ffmpeg 的详细日志
- 可以独立测试推流是否成功
- 便于调试连接问题

---

## 🔍 问题排查

### 常见问题速查表

| 问题 | 症状 | 解决方案 |
|------|------|----------|
| **MediaMTX 端口占用** | `bind: Only one usage of each socket address` | [查看详情](#问题-1-mediamtx-端口占用) |
| **ffmpeg 找不到** | `ffmpeg: 无法识别` | [查看详情](#问题-2-ffmpeg-找不到) |
| **推流 10 秒后断开** | `i/o timeout` | [查看详情](#问题-3-推流超时断开) |
| **后端无法连接** | `无法打开 RTMP 流` | [查看详情](#问题-4-后端无法连接-rtmp-流) |
| **接收 0 帧** | 测试报告显示 0 帧 | [查看详情](#问题-5-接收-0-帧) |

---

### 问题 1: MediaMTX 端口占用

**症状**:
```
ERR [RTMP] listen tcp :1935: bind: Only one usage of each socket address
```

**原因**: 端口 1935 被其他进程占用

**解决**:
```powershell
# 1. 查找占用进程
netstat -ano | findstr :1935

# 2. 终止进程 (PID 为上一步查到的进程ID)
taskkill /F /PID <PID>

# 3. 重启 MediaMTX
cd mediamtx_v1.15.4
.\mediamtx.exe
```

---

### 问题 2: ffmpeg 找不到

**症状**:
```
ffmpeg : 无法将"ffmpeg"项识别为 cmdlet、函数、脚本文件或可运行程序的名称
```

**原因**: ffmpeg 未安装或不在 PATH 中

**解决**:
```powershell
# 方案 A: 使用 Chocolatey 安装
choco install ffmpeg -y

# 方案 B: 临时添加到 PATH (当前会话)
$env:Path += ";C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin"

# 验证
ffmpeg -version
```

**详细安装**: [FFMPEG_INSTALL.md](./FFMPEG_INSTALL.md)

---

### 问题 3: 推流超时断开

**症状**:
MediaMTX 日志显示:
```
INF [RTMP] [conn] opened
INF [RTMP] [conn] closed: read tcp [...] i/o timeout
```

**原因**: 
1. 测试视频文件过短或格式不兼容
2. ffmpeg 进程输出缓冲区被填满导致阻塞

**解决**:
```powershell
# 1. 检查测试视频
ffmpeg -i test/test_video.mp4

# 2. 使用 test_rtmp_quick.py (已修复缓冲区问题)
cd test
python test_rtmp_quick.py --duration 20

# 3. 或生成新的测试视频
ffmpeg -f lavfi -i testsrc=duration=60:size=640x480:rate=30 -pix_fmt yuv420p test/test_video_new.mp4
```

---

### 问题 4: 后端无法连接 RTMP 流

**症状**:
后端日志显示:
```
[RTMP Worker] 尝试打开 RTMP 流 (尝试 1/5)...
[RTMP Worker] ❌ 无法打开 RTMP 流，等待 2 秒后重试...
```

**原因**: 
1. MediaMTX 未运行
2. 推流未成功建立
3. RTMP URL 不匹配

**排查步骤**:

**1. 检查 MediaMTX 是否运行**
```powershell
netstat -ano | findstr :1935
```
应该看到 `LISTENING` 状态

**2. 检查推流是否成功**
查看 MediaMTX 日志,应该看到:
```
INF [RTMP] [conn ...] is publishing to path 'live/test'
```

**3. 检查 RTMP URL**
确保推流地址和测试脚本中的 URL 一致:
- 推流: `rtmp://localhost:1935/live/test`
- 测试: `--rtmp_url rtmp://localhost:1935/live/test`

---

### 问题 5: 接收 0 帧

**症状**:
```
测试时长: 30.3 秒
接收帧数: 0 帧
⚠️ 测试未通过
```

**原因**: 
1. 推流和捕获的时序问题
2. AI 推理模型未加载
3. WebSocket 连接失败

**解决**:

**1. 使用改进的 test_rtmp_quick.py**
```powershell
# 已优化时序: 推流 8秒 + 捕获初始化 5秒
python test_rtmp_quick.py --duration 30
```

**2. 使用手动分步测试**
```powershell
# 终端 1: 先启动推流
ffmpeg -re -stream_loop -1 -i test_video.mp4 -c:v libx264 -preset ultrafast -f flv rtmp://localhost:1935/live/test

# 等待 5 秒,观察 MediaMTX 日志确认 'is publishing'

# 终端 2: 再运行测试
python test_rtmp_integration.py --rtmp_url rtmp://localhost:1935/live/test
```

**3. 检查后端日志**
在 uvicorn 终端查看是否有错误:
```
[RTMP Worker] ✅ 成功打开 RTMP 流 for quick_test  # 正常
[RTMP Worker] 开始捕获帧，目标帧率: 30 FPS        # 正常
[RTMP Worker] 已捕获 100 帧                        # 正常
```

---

### 完整诊断流程

**如果测试仍然失败,按以下顺序检查**:

```powershell
# 1. 检查 MediaMTX
cd mediamtx_v1.15.4
.\mediamtx.exe
# 应该看到: INF [RTMP] listener opened on :1935

# 2. 检查后端 API
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
# 应该看到: Uvicorn running on http://127.0.0.1:8000

# 3. 检查 ffmpeg
ffmpeg -version
# 应该看到版本信息

# 4. 检查 OpenCV RTMP 支持
python -c "import cv2; print(cv2.getBuildInformation())" | findstr "FFmpeg"
# 应该看到: FFmpeg: YES

# 5. 手动测试推流
cd test
ffmpeg -re -i test_video.mp4 -c:v libx264 -f flv rtmp://localhost:1935/live/test
# MediaMTX 应该显示: is publishing to path 'live/test'

# 6. 手动测试捕获
python test_rtmp_integration.py --rtmp_url rtmp://localhost:1935/live/test
# 应该开始接收帧
```

---

## 📖 参考文档

### 环境搭建
- [MediaMTX 安装指南](./MEDIAMTX_SETUP.md) - RTMP 服务器安装和配置
- [FFmpeg 安装指南](./FFMPEG_INSTALL.md) - 推流工具安装

### 测试脚本
- `test/test_rtmp_quick.py` - 一键自动化测试 (**推荐使用**)
- `test/test_rtmp_integration.py` - 集成测试 (依赖外部推流)

### 架构文档
- [AI 推理架构](./docs/AI_INFERENCE_ARCHITECTURE.md) - 四队列架构说明
- [性能优化](./test/PERFORMANCE_OPTIMIZATION.md) - 性能调优指南

---

## 🎯 测试成功标志

### MediaMTX 日志
```
INF [RTMP] [conn [::1]:xxxxx] opened
INF [RTMP] [conn [::1]:xxxxx] is publishing to path 'live/test', 2 tracks (H264, MPEG-1/2 Audio)
INF [RTMP] [conn [::1]:yyyyy] opened  # 后端连接
```

### 后端日志 (uvicorn 终端)
```
[RTMP Worker] 启动捕获线程 for quick_test: rtmp://localhost:1935/live/test
[RTMP Worker] 尝试打开 RTMP 流 (尝试 1/5)...
[RTMP Worker] ✅ 成功打开 RTMP 流 for quick_test
[RTMP Worker] 开始捕获帧，目标帧率: 30 FPS
[RTMP Worker] 已捕获 100 帧 for quick_test
```

### 测试输出
```
⏱️  已运行 10s | 已接收 300 帧 | 平均 30.0 FPS
⏱️  已运行 20s | 已接收 600 帧 | 平均 30.0 FPS
⏱️  已运行 30s | 已接收 900 帧 | 平均 30.0 FPS

============================================================
📊 测试报告
============================================================
测试时长: 30.3 秒
接收帧数: 900 帧
平均帧率: 29.7 FPS
目标帧率: 30 FPS
✅ 测试通过 (帧率达标)
============================================================
```

---

## 💡 最佳实践

### 日常开发测试
```powershell
# 使用一键测试快速验证
cd test
python test_rtmp_quick.py --duration 20
```

### 问题调试
```powershell
# 使用手动分步测试
# 1. 启动 MediaMTX 和后端
# 2. 手动推流 (观察日志)
# 3. 运行 test_rtmp_integration.py
```

### 性能测试
```powershell
# 长时间运行测试
python test_rtmp_quick.py --duration 300  # 5分钟

# 监控队列状态
curl http://localhost:8000/ai/status
```

---

## 📞 获取帮助

如果遇到未在本指南中列出的问题:

1. **检查日志**: 查看 MediaMTX 和后端的详细日志
2. **查看文档**: 阅读 [MEDIAMTX_SETUP.md](./MEDIAMTX_SETUP.md) 和 [FFMPEG_INSTALL.md](./FFMPEG_INSTALL.md)
3. **调试模式**: 使用手动分步测试定位具体问题
4. **测试工具**: 使用 VLC 或 ffplay 独立验证 RTMP 流

---

**最后更新**: 2025-11-30

**版本**: CleanSightBackend v1.0 (fix/RTMP 分支)
