# 推理快照、时序叠字、viz 去重时间戳迁成 topic

> **变更状态**：生效中（2026-10-24）——session/mq 重构批 3
> **知识库**：待沉淀

## 概述

CQ 的 `_latest_detection`、`_latest_temporal` 两个槽位与 viz worker 的 `_last_rendered_ts[task_id]` 表迁成 `inference/online/topics.py` 声明的三个 `Latest` topic。
CQ 删掉对应字段、两把锁与 4 个方法。运行时行为不变；viz 的去重表随 run 关闭释放，不再靠每 tick 比对注册表自清理。

## 变更背景

- **承接**：批 2（[20261024_SESSION_BRIDGE](20261024_SESSION_BRIDGE.md)）起每个 CQ 挂着 `cq.sess`，已迁的数据经它读写。
- 提案痛点 #1（推理链路改语义要改 client 包）与 #5（服务内建 task_id 表脱离 run 生命周期）的第一批落点。

## 方案详情

### 全景

```text
写回（DetectionService._write_back_results）── LATEST_DETECTION.set ──▶ viz 读
时序 actor（_tick）─────────────────────────── LATEST_TEMPORAL.set ──▶ viz 读
                                     run_control.stop_run 清前端槽：LATEST_TEMPORAL.clear
viz（送渲染 / 写回渲染结果）────────────────── VIZ_LAST_TS.get / set（自写自读）
```

| topic | 语义 | 替代 | 写方 | 读方 |
|-------|------|------|------|------|
| `LATEST_DETECTION` | `Latest` | `cq.set/get_latest_detection` | 写回 | viz |
| `LATEST_TEMPORAL` | `Latest(default=[])` | `cq.set/get_latest_temporal` | actor；run_control 清空 | viz |
| `VIZ_LAST_TS` | `Latest(default=0.0)` | viz `_last_rendered_ts[task_id]` | viz | viz |

- 声明在 [`app/services/inference/online/topics.py`](../../app/services/inference/online/topics.py)（提案 A7：推理侧自用的 topic 不进 session 包）。
- 写门语义：旧 `set_latest_temporal` 在非 ACTIVE 时放行空列表写；现在 actor 的 `set` 一律只在 ACTIVE 放行，拆除期清空改走 `clear()`（任何状态放行），效果相同。
- `VIZ_LAST_TS` 在 DRAINING 后拒写：拆除期迟到的渲染结果本就被 CQ 写门拒，去重时间戳不推进无影响。
- viz `_tick` 删除「去重表剔除已不在注册表的 task」一段。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| CQ | 6 把 payload 锁 | 4 把 |
| viz 去重表 | 服务内 `task_id → ts`，每 tick 比对注册表剔除 | 挂在 run 上，close 即释放 |

**自测结果**

| 项 | 结果 |
|----|------|
| 写回 fence（`test_writeback_handle_fence`）、算子框架（`test_operator_framework`）、viz（`test_viz_worker_render_proxy`，新增去重时间戳随 run 释放） | passed |
| 全量 `pytest tests/` | 1212 passed |

## 遗留风险 / 后续任务

无（批 4 迁帧窗）。
