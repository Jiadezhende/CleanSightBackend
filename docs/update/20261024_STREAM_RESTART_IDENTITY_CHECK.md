# stream 重启流加乐观核对：停旧 decoder 期间槽位被改过即放弃

> **变更状态**：生效中（2026-10-24）
> **知识库**：待沉淀

## 概述

`StreamService.restart_stream` 在第二段持锁时先核对槽位仍是第一段取到的旧 decoder，不是就放弃并返回 False。
堵住并发 start / stop 插进锁外停旧空档时的漏停 decoder 与 stop 后复活。经 run_control 持任务锁调用时不会命中，现网行为不变。

## 变更背景

- **现状**：[`_restart_stream_impl`](../../app/services/stream/service.py) 分两段持 `StreamService.lock`（全局一把），中间锁外同步停旧 decoder；
  第二段直接按 `task_id` 弹出槽位再建新 decoder，不核对弹出的是谁。
- **问题**：同一 task 的 start / stop 落在两段之间时：

  | 插进来的操作 | 后果 |
  |------------|------|
  | `start_stream` 注册了新 decoder | restart 把它弹出且不停：ffmpeg 不在注册表里、继续写 CQ，无人能停 |
  | `stop_stream` 已弹走旧 decoder | restart 又起一个新 decoder：stop 之后流被复活 |

  现网三个写方法只由 run_control 持任务锁调用，任务锁已串行同 task 的起停，不会触发；本改动让 stream 自身不依赖这条调用约定也不漏停。
- **为何不合并为一段锁**：停旧 decoder 最坏可阻塞约 6s（回收进程、join reader、join stderr 各 2s 超时），持全局锁时一路坏流会卡住所有 task 的起停与 health_monitor 读。

## 方案详情

- `_restart_stream_impl` 第二段锁开头：`self.decoders.get(task_id) is not old_dec` → 记 WARNING `[StreamService] restart_stream skipped`，返回 False。
  `old_dec` 为 None（开头无 decoder）时同理：槽位须仍为空。
- `restart_stream` 改为透传实现的返回值；放弃与失败同为 False，health_monitor 下一周期按当时状态重新决策。

## 变更效果

| 项 | 用例 | 结果 |
|----|------|------|
| 槽位未变照常重启 / 并发 start 换槽放弃 / 并发 stop 清槽放弃 | `tests/test_stream_restart_identity.py`（后两条修复前失败） | 3 passed |
| 全量 `pytest tests/` | — | 1120 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `start_stream` / `stop_stream` 的 docstring 未写「只由 run_control 持任务锁调用」 | 新调用方绕过 run_control 时，同 task 的 start ∥ stop 仍可能新旧 decoder 短暂并存 | 随 `stop_stream` 改同步停时补注 |
| `stop_stream` 仍起 `stop-decoder-{tid}` 线程异步停 | 拆除返回时 ffmpeg 未必已回收 | 改同步停（实测 Windows 各场景 p95 ≤ 21ms），单独一批 |
