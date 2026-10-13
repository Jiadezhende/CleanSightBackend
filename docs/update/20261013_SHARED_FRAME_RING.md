# 新增共享内存帧环 SharedFrameRing：跨进程送帧时队列只传槽位号

> **变更状态**：生效中（2026-10-13）　组件已落地并测绿，尚无调用方（下一批接入推理代理）
> **知识库**：待沉淀

## 概述

新增 [app/services/utils/frame_ring.py](../../app/services/utils/frame_ring.py)，提供以下类型：

| 类型 | 角色 |
|------|------|
| `SharedFrameRing` | 生产方：建 / 分配 / 拷入 / 归还 / 销毁 |
| `SharedFrameRingView` | 消费方：附着并拷出 |
| `RingSpec` / `SharedFrames` | 进队列的小对象 |

本批只新增组件和单测，不改任何调用点。这是 15 路改造第 ④ 阶段的第 1 批，见 [20261013_SCALE_15_STREAMS_PROPOSAL.md](20261013_SCALE_15_STREAMS_PROPOSAL.md)。

## 变更背景

- **现状**：推理代理用 `mp.Queue` 给子进程送帧，帧在主进程里 pickle，再经管道分块写出。每帧在主进程里要花 1.1–1.9ms，持续占用 GIL。15 路时每秒 225 帧，吃掉主进程余量。
- **实测**：满速、无其他负载时，三种送帧方式对比：

| 送帧方式 | 吞吐 | 主进程 CPU / 帧 |
|------|------|------|
| `mp.Queue`（现行） | 434 / 805 帧/s | 1.1–1.9ms |
| TCP 回环（Redis 类方案的下界） | 168 / 186 帧/s | 1.5–1.7ms |
| 共享内存环 | ~9,700 帧/s | 0.10–0.13ms ≈ 一次整帧 memcpy |

## 方案详情

### 全景

```text
生产方：acquire(n) ─► write(slot, frame)×n ─► 队列送 SharedFrames(spec, slots, shapes)
消费方：SharedFrameRingView(spec).read_all(msg) ─► 独立数组（拷出）
生产方：收到答复 → release(slots)；判定对端已死 → reset()；停机 → close()（close + unlink）
```

### 设计约束

- **槽位生命周期只归生产方**：消费方从不归还槽位，回收只发生在生产方收到答复、或判定对端已死的时候。这样不需要跨进程同步空闲表。
- **`acquire` 要么全给、要么不给**：不够就返回 None，由调用方按背压处理；不会出现一批帧只分到一半槽位的情况。
- **消费方一律拷出**：`read` 返回独立数组，消费方不持有共享内存视图，生产方复用槽位不会改到已读出的帧。拷贝发生在消费进程，不占生产方的 GIL。
- **装不下的帧不写**：`fits()` 判 `uint8` 且 `nbytes ≤ slot_bytes`，`write` 遇到装不下的帧抛 `ValueError`。调用方先用 `fits` 分流。
- **命名**：`{prefix}_{pid}_{8 位随机}`，同机多进程不冲突。
- **主进程被 SIGKILL 时**：由 multiprocessing 的 resource_tracker 进程 unlink 这块共享内存。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_frame_ring.py`（要么全给要么不给、归还与 reset、往返且拷出独立、非连续帧、超尺寸与错误 dtype、close 幂等、spawn 子进程跨进程读） | 7 passed |
| 导入门禁登记 `app.services.utils.frame_ring` | passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 处理计划 |
|------------|---------|
| 主进程和 resource_tracker 同时被硬杀（如 `kill -9` 整个进程组）时，`/dev/shm` 会留下残段 | 概率低；残段名带 pid 前缀，可以手工清理。有实际需要再补启动时清理 |
| 尚无调用方 | 下一批接入 `RemoteInferProxy` |
