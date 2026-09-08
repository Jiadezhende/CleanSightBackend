# YOLO_CONFIG_DIR 锁死依赖 import 顺序，detector 顶层 import settings 补上

> **变更状态**：生效中（2026-09-08）
> **知识库**：待沉淀

## 概述

`app/settings.py` 靠 import 副作用把 `YOLO_CONFIG_DIR` 锁进项目内 `.ultralytics`，但 detector 只在函数体内 lazy import settings，晚于 ultralytics 自身 import。绕过 `app.main` / `tests/conftest.py` 直接用 Detector 的进程会把 ultralytics 配置写进用户级目录。改为在 [detector.py](../../app/services/inference/detection/detector.py) 模块顶层 import。

## 变更背景

- **现状**：`os.environ["YOLO_CONFIG_DIR"]` 在 `app/settings.py` 模块级设置；ultralytics 8.3.253 的 `get_user_config_dir()` 在**自己被 import 的那一刻**求值 `USER_CONFIG_DIR` 并冻结，此后改 env 无效。
- **痛点**：`detector.py` 的 `from app.settings import YOLO_RUNS_PROJECT` 写在 `_run_yolo_batch()` 里，位置在 `_ensure_model_loaded()`（内含 `from ultralytics import YOLO`）**之后**。而 `detector.py` 顶层不引用 `app.settings`，模块 import 链上也没有别的地方引用——所以只要调用方不是 `app.main` 或 `tests/conftest.py`（这两处都早早 import settings），env 就来不及设。
- **实测复现**：不经 `app.settings` 直接 `BubbleDetector(...)._ensure_model_loaded()`，`ultralytics.utils.USER_CONFIG_DIR` 解析到 `C:\Users\<user>\AppData\Roaming\Ultralytics`（Linux 对应 `~/.config/Ultralytics`），正是 settings.py 注释想避免的「劫持同机其他模型任务」的那块共享目录。
- **范围**：当前仓库内没有这样的调用方，属隐患而非现存 bug；线上启动路径一直是对的。

## 方案详情

两个 lazy import 的时效性不同，必须分开看：

| 目标 | 生效时机要求 | 原实现 | 是否踩坑 |
|------|-------------|--------|---------|
| `YOLO_CONFIG_DIR`（env） | 必须早于 `import ultralytics` | settings 在 `_run_yolo_batch` 内 lazy import，晚于 `_ensure_model_loaded` | **是**，配置目录漏到用户级 |
| `YOLO_RUNS_PROJECT`（predict 入参） | 只需早于 `predict()` 调用 | 同上，来得及 | 否，runs 一直落在 `.ultralytics` |

### `app/services/inference/detection/detector.py`

把 `from app.settings import YOLO_RUNS_PROJECT` 从 `_run_yolo_batch()` 内提到模块顶层，并注明「必须在顶层」的原因。`app.settings` 只依赖 pydantic，无循环 import 风险。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| `import ...detection.impl.bubble` 后 `USER_CONFIG_DIR` | `%APPDATA%\Ultralytics` | `<repo>\.ultralytics\Ultralytics` |
| `app.main` / pytest 启动路径 | 已正确 | 不变 |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 519 passed |
| 真实推理（`bubble-best.pt`，单帧 640×640） | 跑通；仓库根无 `runs/`，空 save_dir 落在 `.ultralytics/runs/detect/predict` |
| 裸 import detector（不经 settings） | `USER_CONFIG_DIR` 已指向项目内 `.ultralytics` |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `.ultralytics/Ultralytics/settings.json` 里 `runs_dir` 仍指向仓库根 `runs` | 将来若有 `predict`/`track` 调用忘记传 `project=`，`runs/detect/` 会重新长回仓库根 | 暂不处理；目前全仓仅 `_run_yolo_batch` 一处 ultralytics 推理入口，且已传 `project=` |
