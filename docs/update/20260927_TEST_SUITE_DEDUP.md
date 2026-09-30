# 单测去重：删重复 / 恒真 / 测替身的用例，修恒真断言，去真实等待

> **变更状态**：生效中（2026-09-27）
> **知识库**：已沉淀 → [TESTING_MAP.md](../kb/TESTING_MAP.md)（2026-09-30）

## 概述

`tests/` 由 954 例、66s 收到 883 例、14s。删掉跨文件重复、测替身或测试自写逻辑、迁移期护栏类用例；把 7 条「删掉被测逻辑也绿」的断言改成真能红；真实 sleep / 队列轮询 / 重试退避全改 monkeypatch；手搓构造收进 factories / doubles。生产代码只改了 `rtsp_proxy.py` 两个常量的作用域。

## 变更背景

- **现状 / 痛点**：用例全绿但混着大量无效用例——同一路径在两三个文件里各测一遍（retry executor、`hls.list_segments` 在途过滤、m3u8 头部）、测的是 `tests/` 自己写的配方或替身、断言恒真（读超时也返回 `b""` 的 RTSP 拦截用例）、断言旧名字 `not hasattr` 的迁移期护栏。66s 里约 45s 是真实 sleep 与 `SerialTaskQueue` 0.5s 轮询。
- **测试污染**：`TestCli` 在 pytest 主进程里调真 `cli._isolate_cpu`，永久置空 `CUDA_VISIBLE_DEVICES` 并 `torch.set_num_threads(2)`，影响之后所有用例。
- **承接**：只测死代码的用例不在本批，随死代码在下一批一起删。

## 方案详情

### 全景：五类处理

```text
删    重复覆盖 / 测替身或自写配方 / 迁移期护栏         （每条删前核对「被谁覆盖」）
改    恒真断言 → 被测逻辑失效即红                        （monkeypatch 变异验证，未改生产文件）
提速  真实 sleep / 轮询 / 重试退避 → monkeypatch
归一  手搓领域对象 → factories；跨文件替身 → doubles；跨测试文件 import 消除
补缺  删用例暴露的覆盖空白补在现役实现上
```

| 类别 | 落在哪 | 详见 |
|------|--------|------|
| 共享件 | `tests/doubles.py`、`tests/conftest.py` | §1 |
| 恒真断言改写 | gateway / mediamtx_gateway / api_concurrency / pipeline_drop_counters / rounded_rect_roi | §2 |
| 提速 | executor、task_queue、rtsp_proxy、gateway 清理窗口、TestCli | §3 |
| 补覆盖 | cq_drain_fence、task_live_history_api、storage_hls、storage_tasks、import_hygiene | §4 |
| 移出单测 | `integration_tests/test_offline_job_subprocess.py` | §5 |

### 1. 共享件

- `doubles.py` 收 `FakeProc` / `FakeLauncher` / `offline_result`（离线作业子进程）、`FakeDB` / `FakeQuery`（原 history 与 lab 两份，取超集）、`wait_until`。`test_admin_offline_jobs` 不再从 `test_offline_job_service` import。
- `conftest.py` 加显式 fixture `fast_task_queue`：`task_queue._POLL_INTERVAL` 0.5 → 0.01，`stop()` 不再每条队列白等半秒。
- `BrushRulesSegmenter` 去掉无人用的 `min_frames`；`test_cq_immutable_run.py` 删除；`test_rekey_source_ip_shim.py` 改名 `test_client_manager_find_by_source_ip.py`（`find_by_source_ip` 是现役查询轴，不是 shim）。

### 2. 恒真断言改写

| 用例 | 原问题 | 现断言 |
|------|--------|--------|
| mediamtx_gateway 三条拦截 | 客户端读超时也得 `b""` | 写 payload，无回显且远早于超时被关 |
| mediamtx_gateway 目标不可达 | 靠 2s 读超时过，从没观察到 abort | 重试 patch 成 (2, 0.01)，断言恰好尝试 2 次后关闭 |
| gateway_disabled_allows_all | 默认白名单空，开着也不 403 | 先配排除该 IP 的白名单再关开关 |
| api_concurrency 串行 / 不互阻 | 只断言两个 200 / 调用次数 | 事件序 enter → exit → stop_stream；两 task 过 2 方 Barrier |
| pressure_snapshot_silent_when_calm | 空队列循环不执行 | 放一条浅队列再断言静默 |
| rect_partially_offscreen | 只断言 shape | 与整帧基准像素比对 |

`test_gateway` 的 `_init_gw` 原是逐行复制的生产初始化（已漂移），改为 monkeypatch 真实 `settings` 后调 `_ensure_initialized()`；autouse 重置改为前后都跑，封禁状态不再串到别的文件。

### 3. 提速

- executor：文件级 autouse 把 `app.utils.executor.time` 换成记录 sleep 的替身，顺带断言退避序列（persistence `[1.0, 2.0]`）。
- [`rtsp_proxy.py`](../../mediamtx_gateway/rtsp_proxy.py)：函数内局部 `_RETRIES` / `_RETRY_DELAY` 提为模块常量 `_CONNECT_RETRIES` / `_CONNECT_RETRY_DELAY`，值不变，供测试 patch。
- `TestCli` autouse 把 `cli._isolate_cpu` 换成 no-op（消除上文的环境污染，并省 1.1s import torch）。
- `boundary_layers` 的 HTTP handler 用例改为 fixture 临时挂路由、用完摘除，不再往全局 app 永久加路由。

### 4. 补覆盖

- 真 `ClientQueues.take_raw_segment` / `take_processed_segment`：恰好弹 seg_len 帧、不足一段返回 None 且残帧保留、积压逐段拉完（原只有将删的 `test_hls_segment_sweeper` 用真 CQ 测）。
- `/task/history` step 跨度取两轨并集（原只在测试自写的 `_summarise` 上测）。
- `TestEffectiveFps` 加时间戳逆序回落；`TestSelectSegments` 两个边界改写成具名用例。
- 存储根相对路径以项目根为基、与 cwd 无关（从 `test_traceback_segment_finder` 迁到 `test_storage_tasks::TestRootPath`）。
- `test_import_hygiene`：新增「persistence 不 import inference」AST 门禁；`offline.cli` 进 BUDGET，并新增 `FORBIDDEN_APP_IMPORTS`（cli 不得拉起 `app.main` / `app.routers` / 在线推理 / stream）。替代原两条恒真 / 过弱检查。
- 在线工厂 fail-fast 两条从 `test_offline_pipeline` 迁到 `test_operator_framework`。

### 5. 移出单测

`TestRealSubprocess` 真起 CLI 子进程、依赖真实 YAML，按「子进程 I/O 集成-only」移到 `integration_tests/test_offline_job_subprocess.py`（独立脚本，无需后端 / RTSP / DB）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 用例数 | 954 | 883 |
| 全量耗时 | 66.7s | 14.2s |
| 最慢单例 | 12.0s（retry 真 sleep） | 1.2s（首次 import BYTETracker） |
| 删掉被测逻辑仍绿的断言 | 7 条 | 0 |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 883 passed |
| `integration_tests/test_offline_job_subprocess.py` | PASS（约 2.6s） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 只测死代码的用例仍在（HLS 旧写侧、SegmentFinder / Timeline、CircuitBreaker 等） | 死代码仍被「覆盖」，掩盖其无调用方 | 下一批随死代码一起删 |
| `worker_guard.guarded_run` 无单测 | 线程自愈边界层无回归保护 | 待补 |
| `docs/kb/TESTING_MAP.md` 列的若干 factory 名与文件已过时 | 仅文档 | KB 融合时更新 |
| 存疑未动：dispatcher `_admit_to_stage` 接缝、`metadata.json` 只写不读、`gpu_oom_total` 恒 0、平铺布局 TTL 用例（约 2026-10-01 可删） | — | 待 owner 决定 |
