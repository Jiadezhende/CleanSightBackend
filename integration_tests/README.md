# CleanSightBackend 集成测试

完整的端到端集成测试套件，验证从 RTMP 上传到任务终止的完整流程。

## 📁 目录结构

```
integration_tests/
├── __init__.py                    # Python 包标识
├── README.md                      # 本文件
├── test_full_pipeline.py          # 主集成测试脚本
├── utils.py                       # 工具函数（数据库、ffmpeg、API）
└── client_viewer.py               # 独立的推理结果展示客户端
```

## 🎯 测试目标

集成测试验证以下完整流程：

1. **RTMP 推流上传**：通过 MediaMTX 推送本地测试视频
2. **数据库任务加载**：从数据库加载 task_id=0 的任务并启动
3. **实时 AI 推理**：后端捕获 RTMP 流并进行 AI 推理
4. **WebSocket 推送**：客户端通过 WebSocket 接收推理结果
5. **HLS 文件保存**：验证 database 目录下的视频段和关键点文件
6. **任务终止**：正确终止任务，更新数据库状态，释放资源

## 🔧 前置条件

### 1. 环境依赖
- **Python 3.8+** 和虚拟环境 `.venv`
- **MediaMTX** 运行在 `localhost:1935`
- **后端 API** 运行在 `localhost:8000`
- **ffmpeg** 已安装（Chocolatey 或系统 PATH）
- **测试视频** 存在：`test/test_video.mp4`

### 2. 启动服务

**终端 1: 启动 MediaMTX**
```powershell
cd mediamtx_v1.15.4
.\mediamtx.exe
```

**终端 2: 启动后端 API**
```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

### 3. 数据库准备

确保数据库中存在 `task_id=0` 的任务（或测试脚本会自动创建）。

## 🚀 使用方法

### 运行完整集成测试

```powershell
# 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 运行测试（默认 30 秒）
python integration_tests/test_full_pipeline.py

# 自定义参数
python integration_tests/test_full_pipeline.py --task_id 0 --duration 60 --client_id my_test
```

### 参数说明

- `--task_id`: 要测试的任务 ID（默认: 0）
- `--client_id`: 客户端标识符（默认：172.16.77.221，请注意推流地址应与客户端id一致）
- `--duration`: 测试时长秒数（默认: 30）
- `--rtmp_url`: RTMP 推流地址（默认: rtmp://localhost:1935/live/test）
- `--video_path`: 测试视频路径（默认: test/test_video.mp4）

---

**最后更新**: 2025-12-03
