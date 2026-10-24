# session 迁移桥：start_run / stop_run 同步注册 session，CQ 挂 `cq.sess`

> **变更状态**：生效中（2026-10-24）——session/mq 重构批 2
> **知识库**：待沉淀

## 概述

`start_run` 在建 CQ 前 `session_service.open` 一个 `Session` 并挂到 `cq.sess`；`stop_run` 封闸时同步 `seal`，注销 CQ 后 `close` session。
此后各批把 CQ 槽位逐个迁成 topic，已迁的数据经 `cq.sess.channel(T)` 读写。对外接口与日志不变。

## 变更背景

- **承接**：批 1（[20261024_SESSION_CORE](20261024_SESSION_CORE.md)）落了 session 基建，尚无调用方。
- 提案 D3 定的迁移方式是桥接分批：两套结构并存期间只靠 `cq.sess` 一个字段相连，批 7 删桥。

## 方案详情

### 全景

```text
start_run（持任务锁）
  runs.allocate → session_service.open(SessionInfo) → ClientQueues(..., sess=sess)   ← CQ 构造失败即 close(sess)
  → client_service.set → start_workflow → start_stream         任一步失败 → stop_run(expected=cq) 回滚
stop_run（持任务锁）
  fence 核对 → cq.to_draining() + session_service.seal(cq.sess)
  → 停 decoder → 结算 / 清前端槽 / 交出残帧
  → client_service.remove(cleanup) → session_service.close(cq.sess)   两步各自 try
```

| 改动 | 落在哪 |
|------|--------|
| CQ 新增 `sess` 入参与属性；裸建时现造不入表的 session；`to_draining` / `close` 同步 session 状态 | `app/services/client/queues.py` |
| `start_run` 注册 session，`stop_run` seal / close | `app/services/run_control/service.py` |
| 每个用例清空 session 注册表 | `tests/conftest.py` |

- `cq.task_started_at` 取 `sess.info.started_at`，两边同一个参考钟。
- CQ 镜像状态是桥接期的过渡：测试与 HealthMonitor 现在都直接操作 CQ 状态，镜像保证已迁 topic 的写门与 CQ 写门同步关闭。批 7 删桥后状态只经 `session_service` 切换。
- `session_service.close` 带身份核对：fence 未命中整段放弃时不碰任何 session；命中时摘的就是该 CQ 的 session。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_session_bridge.py`：注册、封闸先于停 workflow、拆完出表且 CLOSED；fence 未命中新旧 session 都不动；CQ 状态镜像 | passed |
| `test_start_rollback`：回滚后 client 与 session 注册表都无该 task | passed |
| 全量 `pytest tests/` | 1211 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 桥接期 CQ 与 session 两套状态 | 只经 CQ 改状态的路径都已镜像；直接调 `sess.seal/close` 的只有 run_control | 批 7 删桥 |
