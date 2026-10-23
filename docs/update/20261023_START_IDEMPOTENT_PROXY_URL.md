# `/start` 幂等判断改为比较改写后的同形 URL，重复调用不再拆分 run

> **变更状态**：生效中（2026-10-23）
> **知识库**：待沉淀

## 概述

`RunControlService.start_run` 判断幂等时，入参 URL 先经 `stream.service.internal_stream_url`（与起流同一改写）再与 decoder 记录的 URL 比较。
修复前，走代理端口的 URL 永远判不等，同一任务每次重复 `/start` 都会停旧 run、另开新 run。

## 变更背景

- decoder 记录的是改写后的内部拉流地址（代理端口 `mediamtx_proxy_port` → `127.0.0.1:mediamtx_internal_port`），
  `start_run` 却拿原始入参去比，入参只要走代理端口，`cur_url == rtsp_url` 就恒为假。
- 后果：前端或上游重复调 `/start`（同 step、同 URL）时触发「重启」语义——旧 run 被拆、新分配一个 run_id。一次流程被拆成多个 run，
  而离线判读只取最新 run，前段录像与检测结果不再参与判读。

## 方案详情

| 改动 | 落在哪 |
|------|--------|
| 新增 `internal_stream_url(url)`：按 settings 套用代理端口改写，起流与幂等比较共用 | [`app/services/stream/service.py`](../../app/services/stream/service.py) |
| 幂等比较改为 `cur_url == internal_stream_url(rtsp_url)` | [`app/services/run_control/service.py`](../../app/services/run_control/service.py) |

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 新增 `test_same_proxy_url_is_idempotent`（修复前失败、修复后通过） | passed |
| 全量 `pytest tests/` | 1117 passed |

## 遗留风险 / 后续任务

无。
