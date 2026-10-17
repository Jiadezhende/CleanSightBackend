# 推理送帧改走共享内存环：主进程每帧送帧成本从 1.1–1.9ms 降到约 0.1ms

> **变更状态**：生效中（2026-10-13）
> **知识库**：已沉淀 → [SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)（2026-10-17）

## 概述

`RemoteInferProxy` 把帧拷进上一批新增的 `SharedFrameRing`（[20261013_SHARED_FRAME_RING.md](20261013_SHARED_FRAME_RING.md)），请求队列只传 `SharedFrames`（槽位号 + 形状）。推理子进程 `run_stages` 按 `ring_spec` 附着并拷出帧。`DetectionService` 按 CQ 声明的帧尺寸和「在途批上限 × 最大批大小」建环。放不进槽位的帧整批退回原 pickle 直送。这是 15 路改造第 ④ 阶段第 2 批，见 [20261013_SCALE_15_STREAMS_PROPOSAL.md](20261013_SCALE_15_STREAMS_PROPOSAL.md)。

## 变更背景

- **现状**：帧经 `mp.Queue` pickle、按管道分块写出，主进程每帧 1.1–1.9ms，期间持续占用 GIL。
- **定位**：本阶段是给主进程省出余量，不是 15 路的硬前提。15 路合成负载下，渲染挪进子进程后，用 Queue 和用共享内存的渲染帧率时有差距、时而持平；但主进程 CPU 始终是共享内存更低（0.47–0.81 核对 0.71–1.36 核）。

## 方案详情

### 全景

```text
dispatcher ─► proxy.submit(batch)
               ├ 锁内：在途检查 → ring.acquire(n)（不够即返回 False，与在途满同一种背压）→ 记 pending + req_slots
               ├ 锁外：ring.write(slot, frame)×n → req_q.put((req_id, stage, SharedFrames, ts))
               └ put 失败 → _rollback：pending 与槽位一并归还
子进程   ─► SharedFrameRingView.read_all → 拷出独立数组 → _infer_models → resp_q
collector ─► _handle_response：pop pending 同时 pop 槽位 → ring.release
失败/停机 ─► _handle_child_failure：清 pending / req_slots → ring.reset()；stop()：清理后 ring.close()
```

| 部件 | 落在哪 |
|------|--------|
| 环生命周期（start 建、stop 关）、submit 分流、回滚、答复归还、失败 reset | [infer_proxy.py](../../app/services/inference/online/detection/infer_proxy.py) |
| `run_stages(..., ring_spec=None)` 附着并拷出 | [stage_worker.py](../../app/services/inference/online/detection/stage_worker.py) |
| 建环参数：`frame_slot_bytes = resize_width × resize_height × 3`，`ring_slots = DEFAULT_MAX_INFLIGHT × max(stage batch_size)` | [detection/service.py](../../app/services/inference/online/detection/service.py) |

### 约束

- **槽位和 pending 同生同灭**：`_req_slots` 与 `_pending` 共用一把锁。凡是清 pending 的路径（答复、回滚、子进程失败、停机），都同时处理槽位。
- **环的大小 ≥ 在途批上限 × 最大批大小**：这样正常情况下，环不会先于在途上限耗尽。万一耗尽，按背压返回 False，帧留在 dispatcher 的 deque 里。
- **放不进槽位就退回 pickle**：如果整批返回 False，dispatcher 会对同一批无限重试，推理就卡死了，所以帧尺寸超出配置时必须退回 pickle 直送。
- **子进程一律拷出**：答复之后主进程才复用槽位；子进程不持有共享内存视图。

| 默认参数 | 值 |
|------|------|
| 单槽 | 640×480×3 = 921,600B |
| 槽位数 | 8 × 4 = 32（`inference_config.yaml` 的 `batch_size: 4`） |
| 共享内存 | ≈ 29MB；原 `mp.Queue` 缓冲上限约 177MB |

## 变更效果

| 维度 | 变更前（`mp.Queue`） | 变更后（共享内存环） |
|------|------|------|
| 主进程送帧 CPU | 1.1–1.9ms / 帧 | 0.10–0.13ms / 帧 |
| 满速吞吐 | 434–805 帧/s | ~9,700 帧/s |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_infer_proxy.py`（新增 6 例：送的是槽位不是数组、答复归还、环耗尽背压、子进程失败 reset、put 失败归还、超尺寸退回 pickle） | 19 passed |
| 真 spawn 冒烟（本地，stage "2" 两个 YOLO，CPU 与 GPU 各一次）：3 批 × 4 帧全部写回，两路检测源齐全、ts 对齐，槽位全数归还，停机干净 | 通过 |
| 全量 `pytest tests/` | 1020 passed |

## 追加（2026-10-13）：槽位归还与重启重置挪进同一把锁

- **问题**：答复归还（`_handle_response`）、回滚归还（`_rollback`）在 `_lock` 内取出槽位号、出锁后才 `ring.release`；子进程失败时 `_handle_child_failure` 在锁内清在途、出锁后才 `ring.reset()`。若归还落在 reset 之后，空闲表会出现重复槽位号（`release` 只追加不查重），重复会一直留到下次重置。之后同一槽位可能同时分给两批请求，后写的帧覆盖先写的，A 路的检测结果在 B 路画面上算出并写回 A 路，不报错。触发窗口是子进程死亡/判 wedge 的那一刻，微秒级。
- **改法**：[infer_proxy.py](../../app/services/inference/online/detection/infer_proxy.py) 三处 `release` / `reset` 全部挪进 `_lock`，与渲染代理的 `_clear_inflight` / `drain` / `_forget` 一致。锁顺序固定为 proxy 锁 → ring 锁，ring 不回调 proxy，不成环。
- **测试**：未新增用例（竞态窗口无法在单测里稳定复现）。全量 `pytest tests/` 1034 passed。

## 遗留风险 / 后续任务

| 风险 / 待办 | 处理计划 |
|------------|---------|
| 只在 Windows 上做过真 spawn 冒烟 | 测试环境启动日志里应能看到 `[RemoteInferProxy] started (... ring=32x921600B)`；推理帧率与改前一致即通过 |
