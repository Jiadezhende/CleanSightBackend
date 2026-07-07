# CleanSight 快速开始指南

本文档提供 CleanSight 后端的上手指南：启动流程、API 调用示例与测试方法。架构/服务细节见知识库 [docs/kb/INDEX.md](kb/INDEX.md)。

## 前置条件

- 已完成 [部署指南](../DEPLOYMENT.md) 的环境配置
- FFmpeg 已安装并在 PATH 中
- MediaMTX 已获取并可执行（二进制不随 git 分发，见部署指南）

---

## 完整启动流程

### 1. 启动 MediaMTX（终端 1）

```bash
cd mediamtx
./mediamtx          # Linux；Windows 用 .\mediamtx.exe
```

### 2. 启动后端（终端 2）

```bash
./start_backend.sh dev      # Linux（加载 .env.dev）
.\start_backend.ps1 dev     # Windows
```

> 验证启动：`GET http://localhost:8000/health/status` 返回 200 即成功。
> 生产已永久关闭 `/docs`、`/redoc`、`/openapi.json`，不能用交互式文档页验证。

### 3. 推流测试视频（终端 3）

```bash
# 向 MediaMTX 推一路 RTMP（MediaMTX 会转成 RTSP 供后端拉取）
ffmpeg -re -stream_loop -1 -i test/clean-test.mp4 \
    -c:v libx264 -preset veryfast -tune zerolatency -f flv rtmp://localhost:1935/live/test
```

### 4. 运行集成测试（终端 4）

```bash
python integration_tests/local_full_pipeline_rtsp.py --task_id 1 --duration 30
```

---

## API 调用示例

运行键为 **int `task_id`**；业务端点对 `task_id`（首选）与旧 `client_id`（= source_ip）双模兼容。

```python
import requests, websocket

# 1. 一步启动：加载任务 + 拉流（统一接口）
resp = requests.post("http://localhost:8000/api/start", json={
    "task_id": 1,
    "rtsp_url": "rtsp://localhost:8004/live/test",
}).json()
# → {"status": "success", "task_id": 1, "rtsp_url": "...", "message": "..."}

# 2. 接收渲染画面：WS 每帧推一个 JPEG data URL 文本
ws = websocket.WebSocket()
ws.connect("ws://localhost:8000/ai/video?task_id=1")
data_url = ws.recv()          # "data:image/jpeg;base64,/9j/4AAQ..."（直接可作 <img src>）

# 3. 轮询增量消息（告警 + 实时信号）
msg = requests.get("http://localhost:8000/task/message/1", params={"since_seq": 0}).json()
# → {"task_id": 1, "max_seq": N, "signals_10s": {...}, "alarms": [...]}

# 4. 终止任务（完整清理：decoder + 推理 + registry + HLS 残段）
requests.post("http://localhost:8000/api/terminate", params={"task_id": 1})
```

### 主要 API 端点

| 端点 | 方法 | 功能 |
| --- | --- | --- |
| `/api/start` | POST | 统一启动（加载任务 + 拉流） |
| `/api/terminate?task_id=` | POST | 统一终止（完整清理） |
| `/ai/video?task_id=` | WebSocket | 实时渲染帧（JPEG data URL 文本） |
| `/task/message/{task_id}?since_seq=` | GET | 前端增量消息（告警 + signals_10s） |
| `/task/{task_id}/alarms` | GET | 告警历史查询 |
| `/health/status` | GET | 系统整体状态 |
| `/health/monitor/stats` | GET | 健康监控统计 |

> 所有 HTTP/WS 请求先经 `GatewayMiddleware`（IP 白名单 / 限流 / 反扫描）。完整端点清单见 [知识库](kb/INDEX.md)。

---

## 返回数据结构

**WebSocket `/ai/video`**：每帧一个文本消息 `data:image/jpeg;base64,<...>`（渲染后帧，前端 ~10ms 轮询接收），非 JSON。

**GET `/task/message/{task_id}`**（字段以路由实际序列化为准）：

```jsonc
{
  "task_id": 1,
  "max_seq": 12,                        // 本次最大告警 seq；下次请求带 since_seq=max_seq 取增量
  "signals_10s": {                      // 按 metric 键组织的近 10s 实时信号（含全量空模板）
    "bubble": { "active": true, "hit_count": 7, "max_conf": 0.92 }
  },
  "alarms": [                           // seq > since_seq 的增量告警
    { "seq": 12, "metric": "bubble", "level": "high", "mode": "realtime",
      "message": "持续产生新气泡…疑似漏气", "timestamp": 1751800000.0 }
  ]
}
```

**GET `/task/{task_id}/alarms`**：`{ "task_id", "total", "alarms": [...] }`（数据库历史告警）。

---

## 测试方法

```bash
# 本地完整流程（30s；--no-window 无窗口）
python integration_tests/local_full_pipeline_rtsp.py --task_id 1 --duration 30

# 远程服务器
python integration_tests/remote_full_pipeline_rtsp.py --task_id 1 --duration 60 --server <host>

# 并发压力（10 任务）+ 清理残留
python integration_tests/stress_test.py --max-tasks 10 --duration 60
python integration_tests/cleanup_processes.py

# 断线重连
python integration_tests/test_reconnect_success.py --task_id 1
python integration_tests/test_reconnect_timeout.py --task_id 1
```

---

## 相关文档

- [部署指南](../DEPLOYMENT.md) — 环境配置
- [知识库](kb/INDEX.md) — 架构、服务内部、API 清单、配置等描述性内容的单一入口
