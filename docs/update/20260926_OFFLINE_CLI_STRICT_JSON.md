# 离线 CLI 增加 `--strict` / `--json`

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

`python -m app.services.inference.offline.cli run` 新增两个参数：
- `--strict`：透传 `OfflineRunSpec(strict=True)`，只跑精确配置了 offline 的 stage，不回落 MOCK。
- `--json`：在 stdout 最后一行输出结果 JSON `{status, producer, segment_count, message}`；失败时 `status="error"`，退出码为 1。

不加这两个参数时，输出和原来完全一样。退出码 0 的状态从 completed / skipped 扩展为 completed / skipped / superseded。

## 变更背景

离线作业服务（下一批）会以子进程方式调用 CLI，需要满足两点：
- 能解析出结果，不用去猜人读格式的输出。
- 只跑配置了 offline 的 step。`strict` 原本就是为作业服务预留的，此前没有生产调用方。

## 方案详情

| 点 | 做法 |
|----|------|
| 输出 JSON 保持 `ensure_ascii` | 子进程的 stdout 按平台代码页编码（Windows 为 GBK），只有纯 ASCII 能被父进程无损解析 |
| 错误也输出 JSON | 父进程直接从 `message` 取出错原因，不必翻 stderr |

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `TestCli` 新增 3 条（json 末行、json 错误、strict 透传） | passed |
| 真实子进程 `cli run --task-id 99999999 --step-id 2 --strict --json` | 1.6 s，末行合法 JSON，状态为 skipped |
| 全量 `pytest tests/` | 935 passed |
