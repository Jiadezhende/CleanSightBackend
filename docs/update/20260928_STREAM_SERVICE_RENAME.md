# stream 入口改名 service.py，删除 log_call 装饰器

> **变更状态**：已完成（2026-09-28）——搬迁改名 + 删除纯日志装饰器，不改运行逻辑
> **知识库**：待沉淀

## 概述

`app/services/stream/manager.py` → `service.py`（类名 `StreamService` 不变）；删除 `app/utils/decorators.py`（`log_call`）及其在
`StreamService.start_stream / stop_stream / restart_stream` 上的三处使用，`tests/test_decorators.py` 随之删除。引用路径同步。

## 变更背景

- **现状**：stream 包入口文件名 `manager.py` 与 services 包骨架（入口 `service.py`）不一致；`log_call` 全仓只有 stream 在用，且它按 `client_id` 提取身份的逻辑对方法调用从未生效（`args[0]` 是 `self`，`StreamService` 没有 `client_id` 属性），产出的只是 `[ENTER]` / `[EXIT]` 两行无身份日志。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 2 波 · B 的第 2 步；建立在同波第 1 步 client 改名（`client_service`）之上。

## 方案详情

### 全景：一处搬迁 + 一处删除 + 引用改写

```text
app/services/stream/manager.py  → app/services/stream/service.py   （去掉 3 处 @log_call，logger 名改 __name__）
app/utils/decorators.py         删除；app/utils/__init__.py 去掉 log_call 的导入与导出
tests/test_decorators.py        删除
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 删装饰器前的副作用核对 | `app/utils/decorators.py`（已删） | §1 |
| 引用改写 | `app/services/stream/{__init__,instance}.py`、`tests/test_{reconnect_on_initial_failure,stream_rewrite}.py`、`start_backend.{sh,ps1}`、`.env.example`、`.claude/skills/deploy/references/runtime-config.md`、`integration_tests/test_single_client.py` | §2 |

### 1. `log_call` 只有日志、没有别的副作用

逐项核对后删除：

| 可能的副作用 | 实际 |
|-------------|------|
| 异常改写 / 吞异常 | 无：`except` 里打一行 ERROR 后原样 `raise` |
| 返回值改写 | 无：原样返回 |
| 计时指标 | 无：耗时只拼进日志文本，不写 Prometheus |
| 读配置 | 只在 `skip_in_production=True` 时读 `settings.debug`，stream 三处都没开 |

删除后的可见差异只有：这三个方法调用不再打 `[ENTER]` / `[EXIT]` / `[ERROR] app.services.stream.manager.xxx` 三类行。异常仍由调用方（`run_control` / 健康监控）按原路径记录。

### 2. 引用改写

- `logging.getLogger("app.services.stream.manager")` → `logging.getLogger(__name__)`（= `app.services.stream.service`）；`config/logging.json` 未按该名配置，无需改。
- `patch("app.services.stream.manager.FFmpegDecoder")`、`from app.services.stream.manager import ...` 改为 `stream.service`。
- 部署脚本与 `.env.example`、deploy skill 里指向 `stream/manager.py:_rewrite_rtsp_url` 的注释改为 `stream/service.py`。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 832 passed, 8 skipped（-11 为删除的 `tests/test_decorators.py`） |
| 残留检查 `git grep stream.manager / stream/manager / log_call / decorators` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 起停流少了 `[ENTER]` / `[EXIT]` 日志 | 排查时少两行调用痕迹；关键节点仍有 `[StreamService]` / `run_control` 的 INFO 日志 | 无 |
| `docs/kb/` 仍写 `stream/manager.py` 与 `log_call` | 读 KB 时路径过时 | KB 融合时按本篇改名 |
