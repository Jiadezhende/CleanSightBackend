# WebSocket 测试脚本使用指南

本目录包含三个 WebSocket 测试脚本，用于测试视频流的上传、接收和端到端流程。

## 📁 测试脚本列表

| 脚本 | 功能 | 测试接口 |
|------|------|----------|
| `test_websocket_upload.py` | 上传视频流测试 | `/inspection/upload_stream` |
| `test_websocket_video.py` | 接收视频流测试 | `/ai/video` |
| `test_websocket_e2e.py` | 端到端完整测试 | 上传 + 接收 |

---

## 🚀 快速开始

### 1. 准备工作

确保已安装依赖：
```bash
pip install websockets opencv-python numpy
```

准备测试视频文件：
- 默认使用 `test/test_video.mp4`
- 或者使用 `--video` 参数指定其他视频文件

### 2. 启动服务器

在测试前，先启动 FastAPI 服务器：
```bash
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload
```

---

## 📤 测试1: 上传视频流

测试 `/inspection/upload_stream` 接口，将视频帧上传到服务器。

### 基本使用

```bash
cd test
python test_websocket_upload.py
```

### 高级选项

```bash
# 使用自定义视频文件
python test_websocket_upload.py --video /path/to/video.mp4

# 自定义发送帧率（例如 15 FPS）
python test_websocket_upload.py --fps 15

# 显示预览窗口
python test_websocket_upload.py --preview

# 使用自定义客户端 ID
python test_websocket_upload.py --client-id my_client

# 完整示例
python test_websocket_upload.py \
  --video test_video.mp4 \
  --url ws://localhost:8000/inspection/upload_stream \
  --client-id test_upload_001 \
  --fps 30 \
  --preview
```

### 参数说明

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--video` | `-v` | 视频文件路径 | `test_video.mp4` |
| `--url` | `-u` | WebSocket 服务器地址 | `ws://localhost:8000/inspection/upload_stream` |
| `--client-id` | `-c` | 客户端ID | `test_client_001` |
| `--fps` | `-f` | 发送帧率 | `30` |
| `--preview` | `-p` | 显示预览窗口 | `False` |

### 输出示例

```
============================================================
🧪 WebSocket 视频流上传测试
============================================================
✅ 视频文件信息:
   路径: test_video.mp4
   分辨率: 1920x1080
   原始FPS: 30.00
   总帧数: 900
   时长: 30.00秒
   发送FPS: 30

🔌 正在连接到 WebSocket: ws://localhost:8000/inspection/upload_stream?client_id=test_client_001
✅ WebSocket 连接成功!
📤 开始发送视频帧...

📊 进度: 30/900 帧 | 成功: 30 | 失败: 0 | 实际FPS: 29.85
📊 进度: 60/900 帧 | 成功: 60 | 失败: 0 | 实际FPS: 29.92
...
```

---

## 📥 测试2: 接收处理后的视频流

测试 `/ai/video` 接口，接收服务器推送的处理后的视频帧。

### 基本使用

```bash
cd test
python test_websocket_video.py
```

**注意：** 需要先运行上传脚本或确保服务器正在为指定的 `client_id` 处理视频流。

### 高级选项

```bash
# 指定测试时长（秒）
python test_websocket_video.py --duration 60

# 保存接收到的帧
python test_websocket_video.py --save --output ./output

# 不显示预览窗口
python test_websocket_video.py --no-preview

# 使用自定义客户端 ID（需要与上传端匹配）
python test_websocket_video.py --client-id test_client_001

# 完整示例
python test_websocket_video.py \
  --url ws://localhost:8000/ai/video \
  --client-id test_client_001 \
  --duration 30 \
  --save \
  --output ./test_output
```

### 参数说明

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--url` | `-u` | WebSocket 服务器地址 | `ws://localhost:8000/ai/video` |
| `--client-id` | `-c` | 客户端ID（需与上传端匹配） | `test_client_001` |
| `--duration` | `-d` | 测试持续时间（秒，0=无限制） | `30` |
| `--save` | `-s` | 保存接收到的帧 | `False` |
| `--output` | `-o` | 输出目录 | `./test_output` |
| `--no-preview` | | 不显示预览窗口 | `False` |

---

## 🔄 测试3: 端到端完整测试（推荐）

同时测试上传和接收，模拟真实场景。这是最推荐的测试方式。

### 基本使用

```bash
cd test
python test_websocket_e2e.py
```

### 高级选项

```bash
# 使用自定义视频
python test_websocket_e2e.py --video /path/to/video.mp4

# 保存处理后的帧
python test_websocket_e2e.py --save

# 不显示预览
python test_websocket_e2e.py --no-preview

# 完整示例
python test_websocket_e2e.py \
  --video test_video.mp4 \
  --upload-url ws://localhost:8000/inspection/upload_stream \
  --receive-url ws://localhost:8000/ai/video \
  --client-id test_e2e_001 \
  --fps 30 \
  --save \
  --output ./test_output
```

### 参数说明

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--video` | `-v` | 视频文件路径 | `test_video.mp4` |
| `--upload-url` | | 上传 WebSocket 地址 | `ws://localhost:8000/inspection/upload_stream` |
| `--receive-url` | | 接收 WebSocket 地址 | `ws://localhost:8000/ai/video` |
| `--client-id` | `-c` | 客户端ID | `test_client_e2e` |
| `--fps` | `-f` | 发送帧率 | `30` |
| `--save` | `-s` | 保存处理后的帧 | `False` |
| `--output` | `-o` | 输出目录 | `./test_output` |
| `--no-preview` | | 不显示预览窗口 | `False` |

### 输出示例

```
============================================================
🧪 WebSocket 端到端测试
============================================================
✅ 视频文件信息:
   路径: test_video.mp4
   分辨率: 1920x1080
   原始FPS: 30.00
   总帧数: 900
   时长: 30.00秒

⚙️  测试配置:
   Client ID: test_client_e2e
   上传URL: ws://localhost:8000/inspection/upload_stream
   接收URL: ws://localhost:8000/ai/video
   目标FPS: 30
   预览模式: 开启
   保存输出: 否

🚀 开始测试...

📤 [上传] 正在连接到: ws://localhost:8000/inspection/upload_stream?client_id=test_client_e2e
✅ [上传] WebSocket 连接成功
📥 [接收] 正在连接到: ws://localhost:8000/ai/video?client_id=test_client_e2e
✅ [接收] WebSocket 连接成功
📤 [上传] 进度: 30 帧 | 成功: 30 | FPS: 29.85
📥 [接收] 进度: 30 帧 | FPS: 29.72
...

============================================================
📊 端到端测试统计
============================================================
总耗时:          30.15 秒

【上传】
  发送帧数:      900
  成功帧数:      900
  失败帧数:      0
  成功率:        100.00%
  平均FPS:       29.85

【接收】
  接收帧数:      895
  错误帧数:      0
  处理率:        99.44%
  平均FPS:       29.68

【延迟】
  帧差:          5
  估计延迟:      0.17 秒
============================================================

✅ 端到端测试完成!
```

---

## 🎯 使用建议

### 1. 开发阶段测试流程

#### 方式A: 端到端测试（推荐）
```bash
# 步骤1: 启动服务器
uvicorn app.main:app --reload

# 步骤2: 运行端到端测试
cd test
python3 test_websocket_e2e.py --preview
```

#### 方式B: 分开测试上传和接收
适用于需要独立调试上传或接收功能的场景。

```bash
# 终端1: 启动服务器
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload

# 终端2: 上传测试
cd test
python3 test_websocket_upload.py \
  --video test_video.mp4 \
  --client-id test_client_001 \
  --fps 30 \
  --preview

# 终端3: 接收测试（需要在上传开始后运行）
cd test
python3 test_websocket_video.py \
  --client-id test_client_001 \
  --duration 60
```

**重要提示：**
- ⚠️ 上传和接收的 `--client-id` 必须保持一致
- ⚠️ 先启动上传测试，等待数据流稳定后再启动接收测试
- ⚠️ 接收测试会等待服务器推送数据，如果长时间无数据会超时

### 2. 性能测试

```bash
# 测试高帧率
python3 test_websocket_e2e.py --fps 60

# 测试低帧率（节省资源）
python3 test_websocket_e2e.py --fps 15

# 长时间稳定性测试（无预览）
python3 test_websocket_e2e.py --no-preview
```

### 3. 调试问题

```bash
# 保存输出帧进行分析
python test_websocket_e2e.py --save --output ./debug_output

# 使用不同的客户端ID避免冲突
python test_websocket_e2e.py --client-id debug_client_123
```

---

## ⚠️ 常见问题

### Q1: 连接失败
```
❌ WebSocket 连接错误: [Errno 61] Connection refused
```
**解决方案：** 确保 FastAPI 服务器正在运行。

### Q2: 接收不到数据
```
⏳ [接收] 等待服务器推送数据...
```
**解决方案：** 
- 确保上传端正在发送数据
- 检查 `client_id` 是否匹配
- 检查服务器日志是否有错误

### Q3: 帧率过低
**解决方案：**
- 降低视频分辨率
- 减少 `--fps` 参数值
- 检查网络延迟

### Q4: 内存占用过高
**解决方案：**
- 不要使用 `--save` 参数
- 关闭预览窗口（使用 `--no-preview`）
- 降低帧率

---

## 📝 输出文件结构

当使用 `--save` 参数时，输出文件结构如下：

```
test_output/
├── e2e_test_client_e2e_20231124_143022/
│   ├── processed_000001.jpg
│   ├── processed_000002.jpg
│   └── ...
└── session_test_client_001_20231124_143500/
    ├── frame_000001.jpg
    ├── frame_000002.jpg
    └── ...
```

---

## 🔧 高级用法

### 多客户端并发测试

```bash
# 终端1
python test_websocket_e2e.py --client-id client_001 &

# 终端2
python test_websocket_e2e.py --client-id client_002 &

# 终端3
python test_websocket_e2e.py --client-id client_003 &
```

### 自动化测试脚本示例

```bash
#!/bin/bash
# test_all.sh

echo "🧪 开始自动化测试..."

# 测试不同帧率
for fps in 15 30 60; do
    echo "测试 FPS: $fps"
    python test_websocket_e2e.py --fps $fps --client-id "test_fps_${fps}" --no-preview
    sleep 5
done

echo "✅ 所有测试完成"
```

---

## 📊 性能指标

测试脚本会报告以下指标：

- **发送帧数**: 成功发送到服务器的帧数
- **接收帧数**: 从服务器接收的处理后帧数
- **成功率**: 成功处理的帧占总帧数的百分比
- **平均FPS**: 实际的平均帧率
- **延迟**: 上传和接收之间的时间差

---

## 📚 相关文档

- FastAPI WebSocket 文档: https://fastapi.tiangolo.com/advanced/websockets/
- OpenCV Python 文档: https://docs.opencv.org/

---

## 💡 提示

1. **预览窗口**: 按 `q` 键可以随时退出预览
2. **中断测试**: 按 `Ctrl+C` 可以中断测试
3. **日志输出**: 测试过程中会实时显示进度和统计信息
4. **客户端ID**: 确保上传和接收使用相同的 `client_id`

---

如有问题或建议，请联系开发团队。
