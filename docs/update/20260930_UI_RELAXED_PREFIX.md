# /ui-f3m8 静态页并入网关宽松前缀

> **变更状态**：生效中（2026-09-30）
> **知识库**：已沉淀 → [SERVICE_GATEWAY_MEDIAMTX.md](../kb/SERVICE_GATEWAY_MEDIAMTX.md)、[SERVICE_CONFIG.md](../kb/SERVICE_CONFIG.md)（2026-09-30）

## 概述

- **改了什么**：`gateway_relaxed_prefixes` 默认值加入 `/ui-f3m8`（[app/settings.py](../../app/settings.py)，`.env.example` 注释同步）；[tests/test_gateway.py](../../tests/test_gateway.py) 的生产默认宽松前缀用例加 `/ui-f3m8/admin/`。
- **为什么**：静态页挂载由 `/admin-f3m8/ui`、`/lab-f3m8/ui` 统一迁到 `/ui-f3m8`（[20260925_UI_MOUNT_UNIFY](20260925_UI_MOUNT_UNIFY.md)）时漏同步宽松前缀——旧 admin 页原本被 `/admin-f3m8` 前缀顺带覆盖，迁移后 admin / lab 两页掉进普通配额（60 次/60s + 超限封禁升级），一次开页就拉 index.html + 多个 vendor 资产，刷新几次即被封。
- **过时前缀核对**：现有宽松前缀 `/health`、`/task/message`、`/task/live`、`/task/history`、`/traceback`、`/admin-f3m8`、`/metrics` 均仍对应在役路由，无需删除；`/admin-f3m8` 仍被 admin 页轮询（overview / metrics/json / offline jobs）。
- **KB 待校正**：[SERVICE_CONFIG.md](../kb/SERVICE_CONFIG.md) 三档路径策略里「默认配额含 `/ui-f3m8` 静态页」的结论已失效，`/ui-f3m8` 现属宽松档。
