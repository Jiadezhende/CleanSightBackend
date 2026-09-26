# `app/algorithm/` 收进 `app/services/algorithm/`，算法对外经 service 接口提供

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

试纸比色算法从顶层 `app/algorithm/colorstrip/` 挪到 `app/services/algorithm/colorstrip/`，新增
`app/services/algorithm/service.py` 作为对外接口；`routers/algorithm.py` 只剩请求解析与异常翻译。
HTTP 契约 `POST /algorithm/colorstrip` 不变；CLI 入口改为 `python -m app.services.algorithm.colorstrip.cli`。

## 变更背景

- **现状**：`app/algorithm/` 是 `app/` 下与 `services/` 平级的顶层包，router 直接 import
  `config` / `grader` / `types` 三个算法模块，自己编排「读档 → 解图 → 判定 → 拼结论 → 打拒判日志」。
  当初不放进 services 的理由是「别的 service 若要用，会变成 service → service 依赖」。
- **触发**：比色与主流程（推理 / 录制 / 告警）无关，推理服务不会调用它，上述顾虑不成立；
  业务能力应以 service 形式提供，不散落在 services 外。

## 方案详情

### 全景

```text
POST /algorithm/colorstrip
  → routers/algorithm.py      base64 / data URL → bytes（按 colorstrip_max_image_bytes() 解码前拦超限）
  → services/algorithm/service.grade_colorstrip(bytes, profile)
        config.load(profile)  ──KeyError──→ UnknownProfileError
        grader.imdecode       ──ValueError→ ImageDecodeError
        grader.grade → ColorstripVerdict(ok, passed, code, message)；拒判诊断打日志
  → router：UnknownProfileError → 400 field=profile；ImageDecodeError → 400 field=image_base64
```

| 部件 | 落在哪 |
|------|--------|
| 服务接口 | [`service.py`](../../app/services/algorithm/service.py)：`grade_colorstrip` / `colorstrip_max_image_bytes` / `ColorstripVerdict` / 两个具名异常 |
| 算法实现（原样挪入，只改路径字样） | [`colorstrip/`](../../app/services/algorithm/colorstrip/) |
| HTTP 翻译 | [`routers/algorithm.py`](../../app/routers/algorithm.py) |
| 分层门禁 | `tests/test_import_hygiene.py`：BUDGET 与 `LAYER_PACKAGES` 改登记 `app.services.algorithm`，新增 `service` 一条 |

### 定下来的口径

- **无单例、无 `lifespan()`**：没有状态可管，模块级函数即接口，不进 `app.main` 启动序列。
- **整个 `app/services/algorithm` 仍零 `app.*` 依赖**（白名单只有自己），算法子包依旧能整个拷走单独跑。
- **服务抛具名异常而非裸 `ValueError` / `KeyError`**：router 若宽接 `KeyError`，grader 内部的
  KeyError 类 bug 会被报成「档名写错」的 400，而不是 500。两个异常分别是 `ValueError` / `KeyError` 的子类。

### 行为差异

`profile` 写错**且** base64 同时有问题时，旧实现先报 `profile`，现在先报 `image_base64`（base64 解码移到
读档之前）。两者都是 400，单项错误时行为不变。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_algorithm_router.py` + `tests/test_import_hygiene.py` | 59 passed（基线 58 + 新登记 `service` 预算 1 条） |
| CLI `python -m app.services.algorithm.colorstrip.cli --help` | 正常 |
| 全量 `pytest tests/` | 915 passed, 8 skipped |

## 遗留风险 / 后续任务

无。
