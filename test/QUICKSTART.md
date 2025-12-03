# WebSocket 测试快速开始

## 🎯 测试目标

测试 CleanSightBackend 的两个 WebSocket 接口：
1. `/inspection/upload_stream` - 上传视频帧到服务器
2. `/ai/video` - 接收服务器推送的处理后的视频帧

## 📋 前置要求

### 1. 安装依赖
```bash
pip install websockets opencv-python numpy
```

### 2. 准备测试视频
确保 `test/test_video.mp4` 存在，或准备其他视频文件。

### 3. 启动服务器
```bash
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload
```

## 🚀 三种测试方式

### 方式1: 交互式测试（推荐新手）

```bash
cd test
python3 run_tests_interactive.py
```

按照菜单提示选择测试类型并配置参数。

### 方式2: Bash 脚本测试（推荐）

```bash
cd test

# 端到端测试（最常用）
./run_test.sh e2e --preview

# 上传测试
./run_test.sh upload --preview

# 接收测试
./run_test.sh receive

# 查看帮助
./run_test.sh help
```

### 方式3: 直接运行 Python 脚本

```bash
cd test

# 端到端测试
python3 test_websocket_e2e.py --video test_video.mp4 --preview

# 上传测试
python3 test_websocket_upload.py --video test_video.mp4 --preview

# 接收测试
python3 test_websocket_video.py --client-id test_client_001
```

## 🎬 完整测试流程示例

### 场景A: 端到端一体化测试（推荐）

#### 1. 启动服务器
```bash
# 终端 1
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload
```

#### 2. 运行端到端测试
```bash
# 终端 2
cd test
./run_test.sh e2e --preview

# 或使用 Python 直接运行
python3 test_websocket_e2e.py --video test_video.mp4 --preview
```

#### 3. 观察输出
- 实时显示上传和接收的进度
- 预览窗口显示处理后的视频
- 最终显示统计信息（帧率、成功率、延迟等）

---

### 场景B: 分开测试上传和接收（用于调试）

当你需要分别测试上传和接收功能时，可以在两个终端中分别运行：

#### 1. 启动服务器
```bash
# 终端 1
cd /Users/hmj/projects/CleanSightBackend
uvicorn app.main:app --reload
```

#### 2. 上传视频流
```bash
# 终端 2 - 上传测试
cd test

# 使用 Bash 脚本
./run_test.sh upload --preview

# 或使用 Python 直接运行
python3 test_websocket_upload.py \
  --video test_video.mp4 \
  --client-id test_client_001 \
  --fps 30 \
  --preview
```

#### 3. 接收处理后的视频流
```bash
# 终端 3 - 接收测试（需要在上传开始后运行）
cd test

# 使用 Bash 脚本
CLIENT_ID=test_client_001 ./run_test.sh receive

# 或使用 Python 直接运行
python3 test_websocket_video.py \
  --client-id test_client_001 \
  --duration 60

# 注意：client-id 必须与上传端保持一致！
```

#### 💡 分开测试的要点

1. **client-id 必须一致**：上传和接收使用相同的 client-id
2. **先启动上传**：确保有数据流后再启动接收端
3. **独立测试**：可以只测试上传或只测试接收
4. **调试方便**：分别查看上传和接收的日志输出

#### 示例：完整的分离测试命令

```bash
# 1. 启动服务器（终端1）
uvicorn app.main:app --reload

# 2. 上传视频（终端2）
cd test
python3 test_websocket_upload.py \
  --video test_video.mp4 \
  --client-id my_test_client \
  --fps 30 \
  --preview

# 3. 接收视频（终端3，等上传开始后运行）
cd test
python3 test_websocket_video.py \
  --client-id my_test_client \
  --duration 60 \
  --save \
  --output ./output
```

## 📊 预期输出

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

✅ 端到端测试完成!
```

## 🎨 预览窗口

测试期间，预览窗口会显示：
- 处理后的视频帧（带有检测框、关键点等）
- 实时统计信息（帧数、FPS、时间等）
- AI 推理结果（弯折检测、动作分析等）

按 `q` 键可以随时退出预览。

## ⚙️ 常用配置

### 调整帧率
```bash
# 低帧率（节省资源）
./run_test.sh e2e --fps 15

# 高帧率（性能测试）
./run_test.sh e2e --fps 60
```

### 性能优化选项 ⭐ 新增

```bash
# 默认异步模式（高性能，推荐）
python3 test_websocket_e2e.py --preview

# 同步模式（等待每帧响应，较慢但更安全）
python3 test_websocket_e2e.py --sync-mode --preview

# 自定义JPEG质量（1-100，默认70）
python3 test_websocket_e2e.py --jpeg-quality 60  # 更快
python3 test_websocket_e2e.py --jpeg-quality 80  # 更清晰

# 性能对比测试
# 同步模式 + 高质量（原版本行为，较慢）
python3 test_websocket_e2e.py --sync-mode --jpeg-quality 85

# 异步模式 + 优化质量（优化版本，快5倍）
python3 test_websocket_e2e.py  # 默认配置
```

**性能提示**:
- 异步模式可将上传FPS从 5-8 提升到 28-30
- JPEG质量70在性能和清晰度间取得最佳平衡
- 详见 `PERFORMANCE_OPTIMIZATION.md`

### 保存输出
```bash
# 保存处理后的帧
./run_test.sh e2e --save

# 输出会保存到 test_output/ 目录
```

### 自定义视频
```bash
# 使用自己的视频文件
./run_test.sh e2e --video /path/to/your/video.mp4
```

### 多客户端测试
```bash
# 终端1
CLIENT_ID=client_001 ./run_test.sh e2e &

# 终端2
CLIENT_ID=client_002 ./run_test.sh e2e &

# 终端3
CLIENT_ID=client_003 ./run_test.sh e2e &
```

## 🐛 故障排查

### 问题1: 连接失败
```
❌ WebSocket 连接错误: [Errno 61] Connection refused
```
**解决**: 确保 FastAPI 服务器正在运行（`uvicorn app.main:app --reload`）

### 问题2: 接收不到数据
```
⏳ [接收] 等待服务器推送数据...
```
**解决**: 
- 检查 `client_id` 是否匹配
- 确保上传端正在发送数据
- 查看服务器日志

### 问题3: 帧率低
**解决**:
- 降低视频分辨率
- 减少 `--fps` 值
- 检查 CPU 使用率

### 问题4: 视频文件不存在
```
❌ 视频文件不存在: test_video.mp4
```
**解决**: 
- 准备一个测试视频并放在 `test/` 目录
- 或使用 `--video` 参数指定其他视频

## 📁 文件说明

| 文件 | 说明 |
|------|------|
| `test_websocket_upload.py` | 上传测试脚本 ⭐ 已优化 |
| `test_websocket_video.py` | 接收测试脚本 |
| `test_websocket_e2e.py` | 端到端测试脚本 ⭐ 已优化 |
| `run_test.sh` | Bash 快捷脚本 |
| `run_tests_interactive.py` | 交互式测试启动器 |
| `README_WEBSOCKET_TESTS.md` | 详细文档 |
| `QUICKSTART.md` | 本文档（快速开始） |
| `COMMAND_REFERENCE.md` | 命令速查表 |
| `PERFORMANCE_OPTIMIZATION.md` | 性能优化说明 ⭐ 新增 |

## 📚 更多信息

查看完整文档：
```bash
cat README_WEBSOCKET_TESTS.md
```

查看性能优化说明：
```bash
cat PERFORMANCE_OPTIMIZATION.md
```

## 🚀 v2.0 性能优化亮点

### 主要改进
- ✅ **异步发送模式**: 不等待响应，速度提升5倍
- ✅ **优化JPEG质量**: 默认70%，平衡性能和清晰度
- ✅ **精确时间控制**: 自动补偿处理延迟
- ✅ **性能监控**: 实时显示编码和发送耗时

### 性能对比
| 模式 | 上传FPS | 提升 |
|------|---------|------|
| 原版本（同步+质量85） | 5-8 FPS | - |
| **优化版本（异步+质量70）** | **28-30 FPS** | **4-5倍** |

详细信息请查看 `PERFORMANCE_OPTIMIZATION.md`

## 💡 最佳实践

1. **开发阶段**: 使用 `--preview` 查看实时效果
2. **性能测试**: 使用 `--no-preview` 避免 UI 开销
3. **调试问题**: 使用 `--save` 保存帧进行分析
4. **CI/CD**: 使用 bash 脚本进行自动化测试

## ✅ 测试清单

- [ ] 服务器已启动
- [ ] 依赖已安装
- [ ] 测试视频已准备
- [ ] 运行端到端测试
- [ ] 检查上传成功率 > 95%
- [ ] 检查接收处理率 > 95%
- [ ] 检查平均延迟 < 1秒
- [ ] 预览窗口显示正常
- [ ] AI 推理结果正确

---

**祝测试顺利！** 🎉
