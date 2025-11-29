# WebSocket 测试命令速查表

## 📋 两个接口分开测试

### 🔧 准备工作

```bash
# 1. 安装依赖
pip install websockets opencv-python numpy

# 2. 启动服务器（终端1）
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload
```

---

## 📤 上传测试命令

测试接口：`/inspection/upload_stream`

### 基础命令
```bash
cd test
python3 test_websocket_upload.py
```

### 完整参数命令
```bash
cd test
python3 test_websocket_upload.py \
  --video test_video.mp4 \
  --client-id test_client_001 \
  --fps 30 \
  --jpeg-quality 70 \
  --preview
```

### 常用变体
```bash
# 异步模式（默认，高性能） ⭐ 推荐
python3 test_websocket_upload.py --preview

# 同步模式（等待响应）
python3 test_websocket_upload.py --sync-mode --preview

# 低质量高速度
python3 test_websocket_upload.py --jpeg-quality 60 --preview

# 高质量（较慢）
python3 test_websocket_upload.py --jpeg-quality 85 --preview

# 低帧率测试
python3 test_websocket_upload.py --fps 15 --preview

# 高帧率测试
python3 test_websocket_upload.py --fps 60 --preview

# 自定义视频
python3 test_websocket_upload.py --video /path/to/video.mp4 --preview

# 使用 Bash 脚本
./run_test.sh upload --preview
```

---

## 🚀 性能优化 v2.0 ⭐

### 优化模式对比

```bash
# 原版本行为（同步+高质量，较慢）
python3 test_websocket_upload.py --sync-mode --jpeg-quality 85

# 优化版本（异步+优化质量，快5倍）⭐ 默认
python3 test_websocket_upload.py
```

### 性能参数

| 参数 | 说明 | 推荐值 |
|------|------|--------|
| `--jpeg-quality` | JPEG质量 (1-100) | 60-75 |
| `--sync-mode` | 同步模式开关 | 不使用（默认异步） |

**性能提升**: 异步模式可将上传FPS从 5-8 提升到 28-30！

---

## 📥 接收测试命令

测试接口：`/ai/video`

⚠️ **重要**: 必须先运行上传测试，并且使用相同的 `client-id`

### 基础命令
```bash
cd test
python3 test_websocket_video.py --client-id test_client_001
```

### 完整参数命令
```bash
cd test
python3 test_websocket_video.py \
  --client-id test_client_001 \
  --duration 60 \
  --save \
  --output ./test_output
```

### 常用变体
```bash
# 短时间测试
python3 test_websocket_video.py --client-id test_client_001 --duration 30

# 保存输出帧
python3 test_websocket_video.py --client-id test_client_001 --save

# 无预览模式
python3 test_websocket_video.py --client-id test_client_001 --no-preview

# 使用 Bash 脚本
CLIENT_ID=test_client_001 ./run_test.sh receive
```

---

## 🔄 端到端测试（推荐）

同时测试上传和接收，自动管理两个接口。

### 基础命令
```bash
cd test
python3 test_websocket_e2e.py
```

### 完整参数命令
```bash
cd test
python3 test_websocket_e2e.py \
  --video test_video.mp4 \
  --client-id test_e2e_001 \
  --fps 30 \
  --jpeg-quality 70
```

### 常用变体
```bash
# 默认优化模式（异步+质量70）⭐ 推荐
python3 test_websocket_e2e.py --preview

# 同步模式（原版本行为）
python3 test_websocket_e2e.py --sync-mode --preview

# 带预览
python3 test_websocket_e2e.py --preview

# 保存输出
python3 test_websocket_e2e.py --save --output ./output

# 性能对比测试
python3 test_websocket_e2e.py --sync-mode --jpeg-quality 85  # 慢
python3 test_websocket_e2e.py  # 快5倍

# 使用 Bash 脚本
./run_test.sh e2e --preview
```

---

## 💡 完整的分离测试示例

```bash
# ========== 终端1: 服务器 ==========
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload

# ========== 终端2: 上传测试 ==========
cd /Users/hmj/projects/CleanSightBackend/test
python3 test_websocket_upload.py \
  --video test_video.mp4 \
  --client-id my_test_client \
  --fps 30 \
  --preview

# ========== 终端3: 接收测试 ==========
# 等待终端2的上传开始后，再执行此命令
cd /Users/hmj/projects/CleanSightBackend/test
python3 test_websocket_video.py \
  --client-id my_test_client \
  --duration 60 \
  --save \
  --output ./test_output
```

---

## 🎯 参数对照表

### 上传测试参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--video` | 视频文件路径 | `test_video.mp4` |
| `--url` | WebSocket URL | `ws://localhost:8000/inspection/upload_stream` |
| `--client-id` | 客户端ID | `test_client_001` |
| `--fps` | 发送帧率 | `30` |
| `--preview` | 显示预览窗口 | 否 |
| `--jpeg-quality` | JPEG质量 (1-100) ⭐ | `70` |
| `--sync-mode` | 同步模式 ⭐ | 否 |

### 接收测试参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--url` | WebSocket URL | `ws://localhost:8000/ai/video` |
| `--client-id` | 客户端ID（需与上传端一致） | `test_client_001` |
| `--duration` | 测试时长（秒） | `30` |
| `--save` | 保存接收的帧 | 否 |
| `--output` | 输出目录 | `./test_output` |
| `--no-preview` | 不显示预览 | 否 |

### 端到端测试参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--video` | 视频文件路径 | `test_video.mp4` |
| `--upload-url` | 上传 WebSocket URL | `ws://localhost:8000/inspection/upload_stream` |
| `--receive-url` | 接收 WebSocket URL | `ws://localhost:8000/ai/video` |
| `--client-id` | 客户端ID | `test_client_e2e` |
| `--fps` | 发送帧率 | `30` |
| `--save` | 保存处理后的帧 | 否 |
| `--output` | 输出目录 | `./test_output` |
| `--no-preview` | 不显示预览 | 否 |
| `--jpeg-quality` | JPEG质量 (1-100) ⭐ | `70` |
| `--sync-mode` | 同步模式 ⭐ | 否 |

---

## ⚡ v2.0 性能优化

### 关键改进
- ✅ **异步发送**: 速度提升 **5倍**
- ✅ **优化质量**: JPEG质量70，平衡性能和清晰度
- ✅ **精确控制**: 自动补偿延迟

### 性能对比

| 模式 | 质量 | 上传FPS | 说明 |
|------|------|---------|------|
| 同步 | 85 | 5-8 | 原版本 |
| **异步** | **70** | **28-30** | **推荐⭐** |
| 异步 | 60 | 30+ | 性能优先 |
| 异步 | 85 | 22-25 | 质量优先 |

详见: `PERFORMANCE_OPTIMIZATION.md`
| `--client-id` | 客户端ID | `test_client_e2e` |
| `--fps` | 发送帧率 | `30` |
| `--save` | 保存处理后的帧 | 否 |
| `--output` | 输出目录 | `./test_output` |
| `--no-preview` | 不显示预览 | 否 |

---

## ⚠️ 常见错误

### 错误1: client-id 不匹配
```
⏳ [接收] 等待服务器推送数据...
```
**解决**: 确保上传和接收使用相同的 `--client-id`

### 错误2: 服务器未启动
```
❌ WebSocket 连接错误: [Errno 61] Connection refused
```
**解决**: 先启动 FastAPI 服务器

### 错误3: 接收端先启动
```
⏳ [接收] 等待服务器推送数据...（持续等待）
```
**解决**: 先启动上传端，等待数据流稳定后再启动接收端

---

## 🚀 快捷脚本

### 使用交互式菜单
```bash
cd test
python3 run_tests_interactive.py
```

### 使用 Bash 脚本
```bash
cd test

# 端到端测试
./run_test.sh e2e --preview

# 上传测试
./run_test.sh upload --preview

# 接收测试
CLIENT_ID=test_client_001 ./run_test.sh receive

# 查看帮助
./run_test.sh help
```

---

## 📖 更多文档

- 完整文档: `README_WEBSOCKET_TESTS.md`
- 快速开始: `QUICKSTART.md`
- 本速查表: `COMMAND_REFERENCE.md`
