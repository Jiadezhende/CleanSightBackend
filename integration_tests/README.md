# CleanSight 集成测试

模拟真实使用场景，观察系统能否正常执行。测试时直连运行中的后端服务，无 mock。

## 前置条件

| 依赖 | 说明 |
|---|---|
| 后端服务 | `http://{server}:{api-port}` 可访问（默认端口 8000，测试环境常用 8100） |
| MediaMTX / RTSPProxy | RTSP 服务运行于 `rtsp://{server}:{rtsp-port}`（默认 8004，测试环境常用 8104） |
| FFmpeg | 使用项目内置 `.ffmpeg/bin/`（由 `install.sh` / `install.ps1` 部署，与后端同源 `settings.ffmpeg_path`）；可用 `CLEANSIGHT_FFMPEG_PATH` 覆写 |
| 测试视频 | `test/test_video.mp4`（可通过 `--video_path` 覆盖，如 `test/clean-test.mp4`） |
| 数据库任务 | `task_id` 不存在则脚本自动创建（结束自动删除）；已存在且 `current_step` 不一致会 fail-fast 报错（见下） |

> 运行解释器：仓库使用 `.venv`。示例里写 `python`，按需替换为 `.venv/bin/python`（macOS/Linux）。

## 文件结构

```
integration_tests/
├── test_single_client.py   # 单客户端测试（场景 1-9）
├── test_multi_client.py    # 多客户端并发测试（并发跑场景 1）
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

### 两个正交维度

测试由两个相互独立的维度控制，可自由组合：

- **`--scenario`** 决定「**怎么跑**」——生命周期行为（正常 / 断流重连 / 延迟推流 / 不 terminate …）
- **`--current-step`** 决定「**跑什么**」——任务阶段，路由到对应推理 workflow：
  - `1` → LEAK（测漏，默认）
  - `2` → CLEAN（清洁）
  - 其它任意值 → MOCK（兜底透传）

例如 `--scenario 2 --current-step 2` = 在 CLEAN 阶段下测断流重连。

### 通用参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--scenario` | 必填 | 场景编号 1-9 |
| `--task_id` | 必填 | 数据库任务 ID（不存在则自动创建并在结束时删除） |
| `--current-step` | 随场景 | 任务阶段（1=LEAK / 2=CLEAN / 其它=MOCK）；显式指定覆盖场景默认 |
| `--server` | `localhost` | 服务器地址（本地/远程均可） |
| `--api-port` | `8000` | 后端 HTTP/WS API 端口 |
| `--rtsp-port` | `8004` | RTSPProxy 推流端口 |
| `--duration` | `60` | 运行时长（秒），从 start 到 terminate |
| `--video_path` | `test/test_video.mp4` | 测试视频 |
| `--fps` | `30` | 推流帧率 |
| `--no-window` | 关闭 | 禁用 OpenCV 可视化窗口，改为打印浏览器 viewer URL |
| `--mode` | `no-stream` | 仅场景 5：`no-stream` / `no-terminate` |
| `--stream-delay` | `10` | 仅场景 6：推流延迟秒数，须 < 重连窗口 25s |

> **task_id 复用说明**：脚本仅在 `task_id` 不存在时自建（并自动清理）。若该 id 已存在于库中：
> - `current_step` 与本次请求一致 → 复用，不清理；
> - 不一致 → **fail-fast 报错退出**（避免「以为测 CLEAN 实际跑了 LEAK」，也不擅自改写可能是真实任务的数据）。
> 跑临时验证时，建议用一个未占用的 id，让脚本自建自删。

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

验证 MediaMTX 正常接收流，且后端不受无关推流影响（不做后端健康检查）。

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

#### 场景 6：延迟推流（健康监控自动重连，Bug 2 验证）

```
先 /api/start（流未就绪，预期失败）→ 等待 stream-delay → 推流 → 验证健康监控自动重连 → terminate
```

验证「拉流早于推流」时，decoder 已注册、健康监控在约 5s 后进入重连模式并自动 `restart_stream`。
`--stream-delay` 须 < 重连窗口 25s。后端日志路径：
`Initial start failed` → `RECONNECT MODE` → `restart_stream` → `Stream restarted successfully`。

```bash
python integration_tests/test_single_client.py --scenario 6 --task_id 1 --stream-delay 10 --duration 30
```

---

#### 场景 7：CLEAN 阶段透传（验证不黑屏）

`--scenario 1 --current-step 2` 的预设别名：标准生命周期跑 CLEAN 阶段，验证帧透传不黑屏。
后端日志关键字：`InferWorker-CLEAN 线程正常运行`。

```bash
python integration_tests/test_single_client.py --scenario 7 --task_id 1 --duration 30
# 等价于：--scenario 1 --current-step 2
```

---

#### 场景 8：MOCK 阶段透传（验证不黑屏）

`--scenario 1 --current-step 未知阶段` 的预设别名：无效 current_step 时 fallback 到 MOCK stage，验证帧透传不黑屏。
后端日志关键字：`未知的 current_step，路由到 MOCK stage`、`InferWorker-MOCK 线程正常运行`。

```bash
python integration_tests/test_single_client.py --scenario 8 --task_id 1 --duration 30
```

---

#### 场景 9：current_step 切换（LEAK → CLEAN）

```
start(step=1/LEAK) → 运行 phase1 → DB 改 step=2 → 再次 start → 全量重建 → CLEAN stage
```

验证：第二次 `/api/start` 因 step 变化触发全量重建（非幂等），stage 由 LEAK 切到 CLEAN，流保持连续。
后端日志：第一次 `InferWorker-LEAK`，第二次 `performing full cleanup before restart` → `InferWorker-CLEAN`。

```bash
python integration_tests/test_single_client.py --scenario 9 --task_id 1 --duration 60
```

---

### 远程服务器

添加 `--server <ip>` 切换远程；测试环境通常还要带 `--api-port` / `--rtsp-port`：

```bash
# 远程测试环境（API 8100 / RTSP 8104），CLEAN 阶段，推 clean-test.mp4，无窗口
python integration_tests/test_single_client.py \
  --scenario 1 --current-step 2 \
  --task_id 9001 \
  --server 111.14.140.60 --api-port 8100 --rtsp-port 8104 \
  --video_path test/clean-test.mp4 \
  --no-window --duration 60
```

> **注意**：远程测试时，FFmpeg 推流至 `rtsp://{server}:{rtsp-port}/live/{client_id}`，后端从自己的 `127.0.0.1:{rtsp-port}` 拉流（脚本与后端已自动 rewrite host）。数据库写入目标由后端 `.env`（`CLEANSIGHT_DB_*`）决定，并非 `--server`，确认它与目标后端使用同一个库。

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

无需 Python 服务器，直接在浏览器中打开（支持 `file://`）。`server` 参数需带上 API 端口。

```
# 直接打开（手动填写 server 和 client_id）
integration_tests/viewer.html

# 带 query params 自动连接（注意端口跟随 --api-port）
integration_tests/viewer.html?client_id=test.s1&server=localhost:8000

# 或通过 HTTP 服务访问（支持跨域时更稳定）
python -m http.server 8080
# 然后访问: http://localhost:8080/integration_tests/viewer.html?client_id=xxx&server=localhost:8000
```

查看器功能：
- 实时视频帧（WebSocket `/ai/video`）+ FPS 统计
- **任务识别状态**（轮询 `GET /task/message/{client_id}`，1s 刷新）：检测结果、置信度、时序事件、近期告警
- 系统健康摘要（轮询 `GET /health/status`，2s 刷新）

`--no-window` 模式下，测试脚本会打印可直接打开的 viewer URL（端口取自 `--api-port`）。

---

## 系统时序参考

以下参数来自 `config/health_monitor_config.yaml`：

| 参数 | 值 | 说明 |
|---|---|---|
| `heartbeat_timeout` | 5s | 无帧后判定为"可疑"的时间 |
| `reconnect_interval` | 5s | 每次重连尝试间隔 |
| `max_reconnect_attempts` | 5 | 最大重连次数 |
| `orphan_timeout` | 30s | 孤儿流超时清理时间 |

流断开后约 **30s** 触发自动清理（5s 等待 + 5×5s 重连）。场景 2 的断流间隔（10s）设计在此窗口内，场景 3 利用此机制验证清理路径，场景 6 利用重连窗口验证延迟推流的自动重连。
```