# CleanSight 集成测试

模拟真实使用场景，观察系统能否正常执行。测试时直连运行中的后端服务，无 mock。

## 前置条件

| 依赖 | 说明 |
|---|---|
| 后端服务 | `http://{server}:8000` 可访问 |
| MediaMTX | RTSP 服务器运行于 `rtsp://localhost:8004`（本地）或 `rtsp://{server}:8004`（远程） |
| FFmpeg | 系统 PATH 中可用，或 Chocolatey 安装于默认路径 |
| 测试视频 | `test/test_video.mp4`（可通过 `--video_path` 覆盖） |
| 数据库任务 | 指定 `task_id` 须存在于数据库，或允许脚本自动创建 |

## 文件结构

```
integration_tests/
├── test_single_client.py   # 单客户端测试（场景 1-5）
├── test_multi_client.py    # 多客户端并发测试（场景 6）
├── viewer.html             # 浏览器推理结果查看器
├── utils.py                # 共享工具（FFmpegController, APIClient, DatabaseHelper）
├── client_viewer.py        # OpenCV 实时推理帧查看器（InferenceViewer）
├── cleanup_processes.py    # 清理残留进程的工具脚本
├── visualize_inference.py  # 独立的推理结果可视化工具
├── logs/                   # 多客户端测试子进程日志
└── deprecated/             # 旧版测试脚本（已被上述文件替代，仅供参考）
```

---

## 单客户端测试 `test_single_client.py`

### 通用参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--scenario` | 必填 | 场景编号 1-5 |
| `--task_id` | 必填 | 数据库任务 ID（不存在则自动创建） |
| `--server` | `localhost` | 服务器地址（本地/远程均可） |
| `--duration` | `60` | 运行时长（秒），从 start 到 terminate |
| `--video_path` | `test/test_video.mp4` | 测试视频 |
| `--fps` | `30` | 推流帧率 |
| `--no-window` | 关闭 | 禁用 OpenCV 可视化窗口 |

### 场景说明

#### 场景 1：正常使用

```
推流 → /api/start → 等待 duration → /api/terminate
```

验证完整正向流程：FFmpeg 推流、后端推理、可视化、正常退出。

```bash
python integration_tests/test_single_client.py --scenario 1 --task_id 1 --duration 30
```

---

#### 场景 2：断流后重连成功

```
推流 → start → 推流 phase1 → 断流 10s → 重新推流 → terminate
```

断流间隔（10s）< 自动清理阈值（25s），后端应自动重连，无需重新调用 `/api/start`。
建议 `--duration >= 50s`（phase1≥15s + gap10s + phase2≥15s + 稳定5s）。

```bash
python integration_tests/test_single_client.py --scenario 2 --task_id 1 --duration 60
```

---

#### 场景 3：断流后重连失败（自动清理）

```
推流 → start → 推流 phase1 → 永久断流 → 观察后端自动清理（~30s）
```

不调用 `/api/terminate`，通过轮询 `/health/status` 确认资源被后端自动清理。
预期：心跳超时（5s）+ 5次重连尝试（5×5s=25s）后触发清理。

```bash
python integration_tests/test_single_client.py --scenario 3 --task_id 2 --duration 60
```

---

#### 场景 4：仅推流

```
FFmpeg 推流 duration 秒，不调任何后端 API
```

验证 MediaMTX 正常接收流，且后端不受无关推流影响。

```bash
python integration_tests/test_single_client.py --scenario 4 --task_id 1 --duration 20
```

---

#### 场景 5：仅 start

通过 `--mode` 选择子场景：

**5a — 忘记推流（`--mode no-stream`，默认）**

```
/api/start（无人推流）→ 等待后端心跳超时 → 自动清理
```

```bash
python integration_tests/test_single_client.py --scenario 5 --task_id 1
```

**5b — 忘记 terminate（`--mode no-terminate`）**

```
推流 → start → 等待 duration → 脚本退出（不调 terminate）
```

FFmpeg 停止后，观察后端孤儿检测与自动清理日志。

```bash
python integration_tests/test_single_client.py --scenario 5 --task_id 1 --mode no-terminate --duration 30
```

---

### 远程服务器

在任意场景中添加 `--server <ip>` 即可切换到远程服务器：

```bash
python integration_tests/test_single_client.py --scenario 1 --task_id 1 --server 117.50.241.174 --duration 30 --no-window
```

> **注意**：远程测试时，FFmpeg 将推流至 `rtsp://{server}:8004/live/{client_id}`，后端从自己的 `localhost:8004` 拉流，两者地址不同，脚本已自动处理。

---

## 多客户端并发测试 `test_multi_client.py`

从数据库查询多个任务，并发运行场景 1，子进程日志写入 `logs/`。
若未禁用窗口，随机挑 2 个客户端显示 OpenCV 可视化。

### 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--server` | `localhost` | 服务器地址 |
| `--duration` | `60` | 每个客户端运行时长 |
| `--max-tasks` | `5` | 最大并发客户端数（从 DB 查询） |
| `--video_path` | `test/test_video.mp4` | 测试视频 |
| `--no-window` | 关闭 | 禁用所有 OpenCV 窗口 |

```bash
# 本地 3 客户端并发，无窗口
python integration_tests/test_multi_client.py --max-tasks 3 --duration 60 --no-window

# 远程 5 客户端并发
python integration_tests/test_multi_client.py --server 117.50.241.174 --max-tasks 5 --duration 60 --no-window
```

---

## 浏览器查看器 `viewer.html`

无需 Python 服务器，直接在浏览器中打开（支持 `file://`）。

```
# 直接打开（手动填写 server 和 client_id）
integration_tests/viewer.html

# 带 query params 自动连接
integration_tests/viewer.html?client_id=test.s1&server=localhost:8000

# 或通过 HTTP 服务访问（支持跨域时更稳定）
python -m http.server 8080
# 然后访问: http://localhost:8080/integration_tests/viewer.html?client_id=xxx&server=localhost:8000
```

查看器功能：
- 实时视频帧（WebSocket `/ai/video`）+ FPS 统计
- **任务识别状态**（轮询 `GET /task/message/{client_id}`，1s 刷新）：检测结果、置信度、时序事件、近期告警
- 系统健康摘要（轮询 `GET /health/status`，2s 刷新）

`--no-window` 模式下，测试脚本会打印可直接打开的 viewer URL。

---

## 系统时序参考

以下参数来自 `config/health_monitor_config.yaml`：

| 参数 | 值 | 说明 |
|---|---|---|
| `heartbeat_timeout` | 5s | 无帧后判定为"可疑"的时间 |
| `reconnect_interval` | 5s | 每次重连尝试间隔 |
| `max_reconnect_attempts` | 5 | 最大重连次数 |
| `orphan_timeout` | 30s | 孤儿流超时清理时间 |

流断开后约 **30s** 触发自动清理（5s 等待 + 5×5s 重连）。场景 2 的断流间隔（10s）设计在此窗口内，场景 3 利用此机制验证清理路径。
