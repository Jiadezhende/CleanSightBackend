# 离线 offline 启用改为 presence 驱动 + resolve_stage 回退补 WARN

> **变更状态**：待提交 PR（2026-07-15）
> **知识库**：待沉淀　<!-- 沉淀时更新 kb 里 offline 配置 schema 段：删除 enabled 开关，改「非空即启用」；resolve_stage 兜底两链路均告警 -->
>
> 承接 [20260715_OFFLINE_CONVERGE.md](20260715_OFFLINE_CONVERGE.md)：同一离线链路的两处收敛式简化，无新增能力。

## 概述

- **改了什么**：两处收紧，均是「去冗余 + 消静默降级」——
  1. offline stage 的启用判据由额外的 `enabled` 布尔开关改为 **presence 驱动**：offline 段非空即视为有意启用，空块 `{}` / 缺省 = 不启用；
  2. 离线 `InferenceConfig.resolve_stage` 未知 step_id 回退 MOCK 前补一行 WARN，与在线 `InferenceManager.resolve_stage` 对齐。
- **为什么改**：
  1. `enabled` 与「配置块是否存在」表达同一件事，冗余；且默认 `False` 使「配全字段却漏写 `enabled: true`」的 offline 段**静默不跑**（silent degradation）。实际配置从没出现过 `enabled: false`，禁用一律靠空块 `{}`，字段半退化。
  2. 非法/未知 step_id → MOCK 是两链路一致的统一兜底，但离线的 config 级解析器原本静默；补 WARN 后，「传了没配的 step_id 已降级 MOCK」在两条链路都可见，避免把「-1 冒烟」和「真打错 step」混看不出。
- **影响面**：仅 `offline/` 配置解析与 `config.py` 的 resolve_stage 日志；在线链路零行为改动（在线 resolve_stage 早有 WARN）。

## 行为契约变化

| 场景 | 改前 | 改后 |
|------|------|------|
| `offline: {}` / 缺省 | 不启用（return None） | 不启用（return None，不变） |
| offline 段字段齐全 | 需额外 `enabled: true` 才启用，漏写则**静默 None** | 非空即启用 |
| offline 段非空但缺 `name/subscribes/class` | 若无 `enabled: true` 静默 None | **fail-fast 抛 ValueError** |
| 未知 step_id → MOCK（离线） | 静默回退 | 回退 + WARN（对齐在线） |

> 临时禁用某 stage 的 offline：留空 `offline: {}` 或整段删除/注释，不再有布尔开关。

## 落点

- [stage_factory.py](../../app/services/inference/stage_factory.py) `create_offline_segmenter`：判据 `not offline or not enabled` → `not offline`；docstring schema 去掉 `enabled` 行。
- [config.py](../../app/services/inference/config.py) `InferenceConfig.resolve_stage`：回退 MOCK 前补 `logger.warning`。
- [config/inference_config.yaml](../../config/inference_config.yaml)：MOCK stage 的 `offline` 删 `enabled: true`，注释改「非空即启用；生产 stage 保持 `{}` 禁用」。
- [tests/test_offline_pipeline.py](../../tests/test_offline_pipeline.py)：`_OFFLINE_OK` 去 `enabled`；`test_disabled_variants_return_none` 拆为 `test_empty_block_returns_none`（`{}`/None→None）+ `test_nonempty_without_required_fail_fast`（非空缺字段→raise）。

## 验证

- `pytest tests/test_offline_pipeline.py tests/test_offline_reservation.py -q` → 42 passed。
