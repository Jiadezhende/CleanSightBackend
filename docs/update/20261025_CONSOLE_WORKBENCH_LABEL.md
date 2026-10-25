# 控制台第 3 批：工作台接入送标——时间轴拖选成片段，只重提失败段，上限取自后端

> **变更状态**：生效中（2026-10-25）
> **知识库**：待沉淀

## 概述

回放工作台新增「送标」面板与时间轴「送标片段」泳道：拖选即生成片段、拖边缘微调、按断点一键拆分；事件面板可「生成前后 5 秒的送标片段」；提交只发未成功的段，失败原因原样显示后端文案；片段按 run 暂存在本标签页，离开有未提交片段的 run 前确认。
后端配套：`GET` / `PUT /lab-f3m8/config` 响应新增 `limits`（送标上限，与 `/submit` 校验同一组 settings）。

## 变更背景

- **现状 / 痛点**（旧 lab 页）：
  - 打点要「设起点 → 设终点 → 加入」三步，选中任务还要再点「加载」；换任务直接清空已选片段。
  - 部分失败后提示「直接重新提交」，会把已成功的段再传一遍，LS 里出现重复任务。
  - 20 段 / 单段 5 分钟 / 总长 30 分钟写死在前端，要和后端 settings 手动同步；失败的 `error_code` 原样显示。
- **触发来源**：控制台改造提案 [20261025_ADMIN_CONSOLE_REDESIGN_PROPOSAL.md](20261025_ADMIN_CONSOLE_REDESIGN_PROPOSAL.md) 第 3 批（B4 + 送标面板）；人已定：打点只留拖选一种方式、快捷键只留播放与前后 1 秒、前端不写业务字段（上限、失败原因以后端为准）。
- **承接**：第 2 批回放工作台（[20261025_CONSOLE_WORKBENCH_PLAYBACK.md](20261025_CONSOLE_WORKBENCH_PLAYBACK.md)）：时间轴泳道接口、`timeline.gaps`、整页锁定的 `run_id`。

## 方案详情

### 全景

```text
进入 run ─▶ sessionStorage 恢复本 run 暂存的片段；GET /lab-f3m8/config → limits、默认 project、LS 地址
打开「送标」面板 ─▶ GET /lab-f3m8/health（可达性只在面板打开与提交前查）
片段来源：
  时间轴「送标片段」泳道拖选 ─┐
  事件面板「前后 5 秒」──────┼─▶ addClip：< 1s 视为误触；超 max_clip_ms 截断；重叠 / 超 max_clips 拒绝
                              └─  跨 timeline.gaps 的片段标「跨越断流，提交会失败」+「按断点拆分」
提交 ─▶ 先查 health ─▶ POST /lab-f3m8/submit（run_id 锁定；只发 status≠ok 的段）
       ─▶ 按 (start_media_ms, end_media_ms) 对回各段：成功锁定并给 LS 链接，失败显示后端 error 原文
离开本 run ─▶ 有未提交片段时确认（片段已暂存，回来恢复）
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 送标上限读口 | `app/services/lab/service.py` `submit_limits`；`app/routers/lab.py` `LabConfigResponse.limits` | §1 |
| 片段状态、泳道、提交、离开保护 | `app/static/console/pages/run-label.js` `useLabel` | §2 |
| 面板与事件面板按钮 | `app/static/console/pages/run.js` | §3 |

### 1. `limits` 读口（B4）

`lab_service.submit_limits()` 返回 `{max_clips, max_clip_ms, max_total_ms}`，`validate_clips` 改为从它取值——前端显示的上限与后端校验是同一组数。GET / PUT `/config` 响应都带上。契约见 [docs/api/lab.md](../api/lab.md#get-lab-f3m8config)。

### 2. `useLabel`

- 片段 `{id, s, e, status: new|sending|ok|fail, lsId, errMsg}`，坐标为 raw 媒体刻度（即 `/submit` 的入参轴）。
- 暂存键 `cs-console-clips:{task}/{step}/{run}`；提交中的段暂存为 `new`。
- 泳道交互：在空白处拖选生成；拖未锁定片段的边缘微调（不越过相邻片段，最短 1s）；点击片段定位到起点；processed 单轨下不能打点（送标只裁 raw）。
- 离开保护经 `router.setLeaveGuard`：只拦「离开本 run」，同一 run 内改 query 不拦；页面卸载时撤掉。

### 3. 面板

- LS 状态行：可达 / 未配置 / 不可达（带后端 error）；project 输入框默认取 `default_project_id`，填 0 交给后端用默认值。
- 片段表：区间、时长、状态；失败或跨断流的段下挂一行原因与「按断点拆分」。
- 提交按钮：首次「提交送标（N 段）」，有已成功段后变「提交未成功的 N 段」；提交期间显示已等待秒数（后端同步执行）。

### 保留项（不改动）

- `/submit` 契约与校验顺序不变；旧 lab 页照常可用。

## 变更效果

| 维度 | 变更前（旧 lab 页） | 变更后 |
|------|------|------|
| 打点 | 设起点 / 设终点 / 加入三步 | 时间轴拖选一步；事件一键前后 5 秒 |
| 部分失败后重提 | 已成功的段会再传一遍 | 只发未成功的段 |
| 跨断流 | 提交后才报 `range_gap` | 提交前标出并可一键拆分 |
| 上限 | 前端写死 | 取自后端 `limits` |
| 换任务 / 刷新 | 已选片段丢失 | 按 run 暂存，离开前确认 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_lab_config_api.py`（新增：GET / PUT 带 `limits`） | 2 passed |
| `tests/test_lab_service.py`（`submit_limits` 与校验同值） | 通过 |
| 全量 `pytest tests/` | 1235 passed |
| 前端 | 本机假后端 + 无头 Edge：拖选两段（一段跨断流）→ 提交后成功段锁定并给 LS 链接、失败段显示后端文案 → 拆分 → 事件生成 36.5–46.5s → 离开时弹确认 → 回来恢复 4 段；未连真实 Label Studio |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `/submit` 同步执行，长批次等待数十秒 | 期间只能看已等待秒数，无逐段进度 | 维持现状；需要时改作业化 |
| 暂存在 sessionStorage | 关掉标签页即丢 | 符合提案（只防误离开）；需要跨会话时再议 |
| 在已有片段内部按下拖动是平移视窗，不会新建 | 想在两段之间紧贴着拖要从空白处起手 | 观察使用反馈 |
