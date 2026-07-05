# stream 模块清理：decoder 自持读循环（删 selector）+ RTSP-only + 子进程回收收敛

> **变更状态**：生效中（2026-07-05）
> **知识库**：待沉淀
>
> 承接：本次建立在 [20260704_RUNKEY_TASKID_LANDING.md](20260704_RUNKEY_TASKID_LANDING.md) 的 `task_id` 换键之上（stream 各方法均以 `task_id:int` 为键）。

## 概述

- **改了什么**：把 `FFmpegDecoder` 的 stdout 读取统一为**decoder 自持的阻塞读线程**（Windows/POSIX 一条路径），删掉 `StreamService` 里整套 selector 多路复用机制；同时清理系统只用 RTSP 后遗留的 `protocol`/RTMP 分支与后端内部 `fps` 死参；并堵上两个资源正确性缺口。
- **为什么改**：stream 模块 review 暴露五处问题——`fps` 死参一路透传却被 decoder 静默丢弃、RTMP/protocol 多协议死逻辑、双平台两套读帧代码（且 selector 跨线程 `register/unregister` vs `select()` 在非 Linux 语义未定义、`self.proc` 有 TOCTOU）、ffmpeg 秒退与已退出进程在 `stop()` 里跳过 `wait()` 导致僵尸滞留、`restart_stream` 异步停旧 decoder 与新 decoder 复用同一 `ca_ready`（无锁 SPSC deque）形成双生产者窗口。
- **影响面**：`app/services/stream/`（decoder + service）、`run_control`、`routers/api`、`health_monitor`（types + monitor）。**前端接口不变**（`StartRequest.fps` 保留于 wire）。

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | `fps` 后端内部死参、`protocol`/RTMP 多协议死逻辑 | 误导/可读性 |
| #2 | 双平台两套读帧路径 + selector 跨线程语义未定义 + `self.proc` TOCTOU | 平台相关/潜在正确性 |
| #3 | 秒退/已退出 ffmpeg 在 `stop()` 跳过 `wait()` → 僵尸滞留 | 资源回收 |
| #4 | `restart_stream` 异步停旧 + 新 decoder 复用同一 `ca_ready` | SPSC 双生产者 |

## 改动详情

### 1. `app/services/stream/decoder.py` — 自持读循环 + 无条件回收

- 新增 [`_reader_loop`](../../app/services/stream/decoder.py)：两平台统一阻塞读 `stdout`，**起始处捕获 `stdout` 本地引用**避开 `stop()` 置 `self.proc=None` 的 TOCTOU；管道被关时 `read` 抛 `ValueError` 或返回 `b""`，均视为流结束正常退出（不自动重启，交 `StreamHealthMonitor`）。删除 `_windows_reader_loop`、`on_stdout_ready`、POSIX 的 `os.set_blocking` 分支。
- `start()` 末尾**无条件**起 `_reader_thread`（原先仅 Windows 起）；秒退检测分支 `raise` 前加 `self.proc.wait(timeout=1.0)` 回收僵尸。
- `stop()`：`wait()` 移出 `if poll() is None`（对已退出进程 `wait` 立即返回并 reap）；close pipe 后**锁外** `join` reader 线程（对称回收，跳过 join 自身线程）。
- `_build_cmd`：RTSP 传输选项内联为模块级 `_RTSP_INPUT_OPTS` 固定前缀，删 `protocol_opts` 形参/字段；ffmpeg 路径直接读 `settings.ffmpeg_path`（去掉 import 期 `FFMPEG_BIN` 快照）。
- 清理 import：`os`、`cv2`、未使用的模块级 `client_manager`。

> **关键决策**：为何用组合（decoder 拥有一个 reader 线程）而非 `class FFmpegDecoder(threading.Thread)`——`start()` 需**同步**完成建流 + 秒退检测并抛 `FFmpegError`/`StreamConnectionError` 供 health monitor 首次感知；若把 Popen 移进 `run()`，调用方就拿不到这个同步失败信号。

### 2. `app/services/stream/service.py` — 删 selector 全套，瘦成注册表

- 删除 `import selectors`、`self.sel`、`self._selector_thread`、`self._stop_event`、`_selector_loop`、`run_once`、`_register_to_selector`、`_unregister_from_selector`、`_cleanup_selector`、`_build_protocol_opts`。`__init__` 不再起线程（连带消除「import 即起 selector 线程」的副作用）。
- `start_stream`/`restart_stream` 去掉 `fps`、`protocol` 形参；不再传 `protocol_opts`。
- `restart_stream`：**旧 decoder 改同步 `stop()`**（锁外先 kill+reap+join，再入锁 cleanup+建新+`start()`）——保证旧 reader join 后新 reader 才写 `ca_ready`，消除 SPSC 双生产者窗口，并同时消除旧进程与新进程/Phase-2 push 在 MediaMTX 同路径的连接竞争。
- `stop_stream` 仍走异步 `stop()`：terminal 路径，无新 run 复用该 CQ，迟到帧由 CQ 写门（DRAINING/CLOSED）拦截，异步安全。
- `get_stream_info` 精简为 `{"url": dec.stream_url}`（协议固定 RTSP、fps 取自 config，重连只需 url）。
- `get_pending_count`：保留跨模块读 `client_manager`（按既定原则接受）；去掉原先无锁读 `self.decoders.get` 的 300 帧日志。

### 3. `run_control.py` / `routers/api.py` — 内部停传 fps，前端契约不动

- [`RunController.start_run`](../../app/services/run_control.py) 删 `fps` 形参；`stream_service.start_stream(task_id=task_id, stream_url=rtsp_url)`。
- [`StartRequest.fps`](../../app/routers/api.py) **保留**（前端仍会发）；api.py 调用处停传 `req.fps`。decoder 输出帧率取自 stream config，抽帧率取自 client config，均与该字段无关。

### 4. `health_monitor/types.py` + `monitor.py` — ReconnectState 收窄

- [`ReconnectState`](../../app/services/health_monitor/types.py) 删 `fps`、`protocol` 字段，保留 `stream_url`。
- `_enter_reconnect_mode` 只取 `stream_info["url"]`；`_handle_reconnecting_client` 调 `restart_stream(task_id=, stream_url=)`。

### 5. `app/services/stream/service.py` — client_manager 惰性导入 + 删死代码 has_stream（收尾）

- **惰性导入**：删掉模块级 `try: from app.services.client import client_manager except ImportError: client_manager = None`——它会把 client 模块内部**任意真实 ImportError** 静默吞成 `None`，导致背压/队列悄然失效。改为 [`_get_client_manager()`](../../app/services/stream/service.py) 调用期导入并缓存：调用发生在运行期（起流/处理帧），各模块已初始化、无导入顺序问题；真实 `ImportError` 直接冒出。核实 `app.services.client` 不 import stream、无实际环，故惰性即可、无需 try/except。三处消费点（`_get_client_queues` / `_restart_stream_impl` / `get_pending_count`）统一改走该 accessor，相应删掉 `if client_manager is None` 死分支。
- **删 `has_stream()`**：全仓无调用方。孤儿检测已改用 `get_all_task_ids()`（看**注册**、不看 `is_alive()`）——重连设计需保留「死掉但仍注册」的 decoder 供重连，用 `has_stream` 的存活判断反而会误清待重连 decoder，故其被有意取代。保留该方法只会诱导误用引回 bug。

### 6. 保留项（不改动）

- `StartRequest.fps`（前端 wire 契约）。
- decoder `self.fps`（取自 config，用于 ffmpeg `scale=...,fps=` 输出帧率，非死参）。
- `get_pending_count` 深入 `client_manager` 的跨模块读（背压查询，刻意保留）。
- `stop_stream` 的异步 stop（terminal 路径正当，不改同步）。

## 数据通道 / 行为说明

| 通道 | 填充 | 消费 | 本次影响 |
|------|------|------|---------|
| ffmpeg stdout → `buffer` | decoder `_reader_loop`（阻塞读，两平台统一） | `_process_frames` | 是（原 POSIX 走 selector 非阻塞读） |
| `ca_ready`（无锁 SPSC deque） | decoder（单生产者） | dispatcher（单消费者） | restart 改同步停旧，杜绝双生产者窗口 |
| `ca_raw` / `_latest_rendered` 等 | 不变 | 不变 | 否 |

## 验证

| 项 | 结果 |
|----|------|
| 相关单测（`test_reconnect_on_initial_failure` / `test_api_concurrency`） | 15 passed |
| 全量 `pytest tests/` | 290 passed |
| 静态 grep（`on_stdout_ready`/`_windows_reader_loop`/`run_once`/`_build_protocol_opts`/`protocol_opts`/残留 fps·protocol 透传） | 无残留 |
| 双平台真流冒烟（起流/断流重连、`ps` 确认无僵尸、Windows 侧） | **待验证**（需真实 RTSP + GPU 环境） |
