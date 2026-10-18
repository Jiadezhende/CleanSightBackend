# admin 离线推理页显示盘上规则告警（过渡期能力，入库后整体退场）

> **变更状态**：生效中（2026-10-18）；过渡期能力，离线告警稳定后改为直接入库，本记录的改动按「退场清单」删除
> **知识库**：待沉淀

## 概述

admin「离线推理」页的作业表加「告警」列，播放区侧栏加规则告警列表（点击跳到违规时刻）；后端新增过渡期端点
`GET /admin-f3m8/offline/alarms/{task_id}/{step_id}` 读 run 的 `alarms.jsonl`。`read_alarms` 改为没核对过返回 `None`，
页面据此区分「未核对」与「无违规疑似」。

## 变更背景

- **现状**：离线规则判定（[20261018_OFFLINE_COMPLIANCE_RULES.md](20261018_OFFLINE_COMPLIANCE_RULES.md)）把违规疑似写进 run 的
  `alarms.jsonl`，但没有任何读取端点；admin 的两处告警（实时页内存环形日志、「告警列表」查 DB）都看不到它。
- **定位（人已拍板）**：盘上告警是过渡期能力，规则与阈值稳定后告警直接入库，走现有 `/task/{task_id}/alarms`。
  因此这次只做最小可用的展示，**控制耦合面**：端点挂在 admin 内部命名空间、字段收窄、前端代码集中一块。

## 方案详情

### 全景

```text
admin 离线推理页 fetchOfflineResult(task, step, track)
  ├ POST /ai/temporal、POST /lab-f3m8/label-probs                        （不变）
  └ fetchOffAlarms → GET /admin-f3m8/offline/alarms/{task}/{step}?track=  单独取，失败不影响分割结果展示
        resolve_timeline(run, track) + storage.read_alarms(run)
        → {checked, items: [{rule_id, metric, alarm_level, alarm_message, media_ms}]}
  侧栏：未核对 / 无违规疑似 / 告警列表（带时刻的点击 seekOff(media_ms)；整段判定的显示「整段」、不可点）
作业表：「告警」列 = OfflineJob.alarm_count
```

| 部件 | 落在哪 |
|------|--------|
| `read_alarms` 没核对过返回 `None` | [`app/storage/inference/_alarm.py`](../../app/storage/inference/_alarm.py) |
| 过渡期端点 | [`app/routers/admin.py`](../../app/routers/admin.py) `get_offline_alarms`；[API 文档](../api/admin.md) 末节 |
| 前端 | [`app/static/admin/index.html`](../../app/static/admin/index.html)：作业表「告警」列、侧栏「规则告警」块、`fetchOffAlarms` |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| A 端点挂 `/admin-f3m8`，字段只给展示要的 5 个 | 只有 admin 页依赖；退场时删端点 + 前端块即可 | 采用 |
| B 挂 `/ai/alarms` 公共命名空间、透出 metadata | 外部前端可能开始依赖，退场要协调；metadata 字段随规则演进变 | 否 |
| C 并进「告警列表」（`/task/{task_id}/alarms`） | 与已入库的正式告警混在一起，误以为已上报；没有 alarm_id / 处理状态 | 否 |

### 退场清单（告警改为直接入库时）

1. 删 `GET /admin-f3m8/offline/alarms` 与 `tests/test_admin_offline_alarms.py`、API 文档末节。
2. 删 admin 页侧栏「规则告警」块与 `fetchOffAlarms` / `offAlarms` / `offAlarmsChecked`（代码里标了「过渡期」）。
3. 规则判定 `check_run` 改为经 alarm 服务上报；`storage.inference` 的 `_alarm.py`、`alarms.jsonl` 视是否还要留离线副本决定去留。
4. 作业表「告警」列可保留（`alarm_count` 来自判定结果，与落点无关）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 盘上告警可见性 | 只能看文件 | admin 离线推理页侧栏可见，点击跳转回看 |
| 未核对 vs 无告警 | 读侧都是 `[]`，分不清 | `checked` 区分 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_admin_offline_alarms.py` | 媒体刻度换算（含断流吸附）、整段判定告警 `media_ms` 为 null、只出 5 个字段、未核对 vs 核对过为空、无录像轨 404 |
| `tests/test_storage_inference.py::TestAlarms` | 缺失读出 `None`、空文件读出 `[]`、读缺失不建目录 |
| admin 页脚本 | `node --check` 语法通过；**未在浏览器里实际打开验证** |
| 全量 `pytest tests/` | 1176 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 页面未在浏览器实测 | Vue 模板错误要到运行时才暴露 | 起 dev 后端打开 `/ui-f3m8/admin/` 离线推理页核一遍 |
| 告警只在侧栏列表，时间线上没打标记 | 回看时要对照列表 | 有需要再在分割段时间线上加标记 |
