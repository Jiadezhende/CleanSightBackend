# 离线 CLI 精简：删 `--strategy` / `--json`，固定输出一行 JSON

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

`offline.cli run` 只剩 `--task-id`、`--step-id`、`--threads` 三个参数，stdout 末行固定输出一行结果 JSON，中文不再转义。
一并删掉的还有 `--strategy`、它背后的 `OfflineRunSpec.strategy`，以及 `StageFactory.create_offline_segmenter` 的 `override_class` 参数。
作业服务的子进程命令同步去掉 `--json`。

## 变更背景

- **`--strategy` 在真实策略之间用不了**：它只替换 `offline.class`，`params` 保持原样。而 YAML 约定换 class 必须同时换 `model_path`，因为权重和特征 recipe 是一一对应的；拿 BiGRU 的权重去 `strict=True` 加载 ASFormer 必然失败。实际上只有测试在用它注入 Boom/Marker 这类测试替身。换策略的正确做法是改 YAML。
- **`--json` 让 CLI 有两种输出格式**：作业服务固定传这个参数，手动跑时输出的是另一种人读格式。一行 JSON 人也能直接读，没必要维护两套分支。
- **承接**：`--strict` 已在上一批（[20260926_OFFLINE_SUBMIT_VALIDATION](20260926_OFFLINE_SUBMIT_VALIDATION.md)）删除；现在未配置的 step 一律报错。

## 方案详情

```text
python -m app.services.inference.offline.cli run --task-id T --step-id S [--threads K]
  → OfflineRunner.run(OfflineRunSpec(task_id, step_id))   # 策略只来自 YAML offline.class
  → stdout 末行：{"status", "producer", "segment_count", "message"}（异常时 status="error"，退出码 1）
```

| 改动 | 落在哪 |
|------|--------|
| 删 `--strategy` / `--json`，固定输出 JSON，`ensure_ascii=False` | [`cli.py`](../../app/services/inference/offline/cli.py) |
| 删 `OfflineRunSpec.strategy` | [`runner.py`](../../app/services/inference/offline/runner.py) |
| 删 `override_class` | [`stage_factory.py`](../../app/services/inference/stage_factory.py) |
| 子进程命令去掉 `--json` | [`service.py`](../../app/services/inference/offline/service.py) |
| 测试替身改为写进注入的 config（`_BOOM` / `_MARKER`） | `tests/test_offline_pipeline.py` |

- **为什么可以不转义中文**：作业服务启动子进程时设置了 `PYTHONIOENCODING=utf-8`，并按 utf-8 解码 stdout，所以不再需要为了父进程解析而输出纯 ASCII。手动在 Git Bash 里跑，中文会显示成乱码，这是终端代码页的问题，`--help` 也一样，与本次改动无关；在 PowerShell 或 cmd 里显示正常。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| `run` 参数 | `--task-id --step-id --strategy --threads --json` | `--task-id --step-id --threads` |
| 输出 | 人读一行，加 `--json` 才是一行 JSON | 固定一行 JSON，中文可读 |
| 换策略 | `--strategy` 或改 YAML | 只能改 YAML（class 和 model_path 一起换） |

**自测结果**

| 项 | 结果 |
|----|------|
| 离线相关三个测试文件 | 72 passed |
| 真实 CLI：`run --task-id 99999999 --step-id 7` | 末行 `status=error`、「step 7 未在推理配置中定义」，退出码 1 |
| 真实子进程链路（`test_end_to_end_with_real_cli`） | 通过，作业服务能解析中文 message |
| 全量 `pytest tests/` | 976 passed |

## 遗留风险 / 后续任务

无。
