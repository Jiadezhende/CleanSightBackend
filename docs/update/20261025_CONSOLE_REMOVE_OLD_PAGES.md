# 控制台第 5 批（下）：删除旧 admin / lab 页面与只被它们用到的前端库

> **变更状态**：生效中（2026-10-25）
> **知识库**：待沉淀

## 概述

控制台五页齐全后删除旧页面：`app/static/admin/`、`app/static/lab/`，以及只被它们引用的 `vendor/element-plus.full.js`、`element-plus.css`、`chart.umd.js`。文档、README、集成测试提示里的旧入口与「参考实现」链接改指控制台。后端接口不变。

## 变更背景

- **承接**：控制台第 1–5 批（[20261025_CONSOLE_SHELL_TASKS.md](20261025_CONSOLE_SHELL_TASKS.md)、[20261025_CONSOLE_WORKBENCH_PLAYBACK.md](20261025_CONSOLE_WORKBENCH_PLAYBACK.md)、[20261025_CONSOLE_WORKBENCH_LABEL.md](20261025_CONSOLE_WORKBENCH_LABEL.md)、[20261025_CONSOLE_OFFLINE_ANALYSIS.md](20261025_CONSOLE_OFFLINE_ANALYSIS.md)、[20261025_CONSOLE_LIVE_SYSTEM.md](20261025_CONSOLE_LIVE_SYSTEM.md)）已覆盖旧两页的全部能力。
- 按开发规范「最后单独删旧」，删除单独成一批。

## 方案详情

### 全景：旧能力 → 控制台去处

| 旧页面 / 功能 | 控制台去处 |
|---|---|
| admin「总览」 | 任务总览（任务）+ 系统运维（健康监控、各路队列深度） |
| admin「实时监控」 | 实时监控（改 `task_id` 模式锁定 run） |
| admin「离线推理」 | 回放工作台「离线分析」+ 分析作业页 |
| admin「指标监控」「延迟测试」 | 系统运维 |
| admin「告警列表」 | 回放工作台「事件」 |
| lab 送标页（含 LS 设置弹窗） | 回放工作台「送标」+ 系统运维（LS 配置、任务清单来源） |

### 改动

| 部件 | 落在哪 |
|------|--------|
| 删旧页面与 3 个 vendor 文件 | `app/static/admin/`、`app/static/lab/`、`app/static/vendor/{element-plus.full.js,element-plus.css,chart.umd.js}` |
| 静态挂载注释 | `app/main.py` |
| 挂载测试只验控制台与 `vue` / `hls` 两个 vendor | `tests/test_static_mount.py` |
| 「参考实现」链接、静态 UI 入口 | `docs/api/{admin,ai,health,lab,traceback,_TEMPLATE}.md` |
| 观测入口 | `README.md`、`docs/QUICK_START.md`、`integration_tests/README.md` 与两个脚本的打印提示 |

### 保留项（不改动）

- 所有后端接口；`/admin-f3m8/clients/{id}/alarms` 等控制台暂不用的端点照常保留。
- `docs/kb/` 不改（随人工融合）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 后台页面 | admin / lab / console 三套并存 | 只有控制台 `/ui-f3m8/console/` |
| vendor | 5 个库 | 2 个（Vue 全局版、hls.js） |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 通过 |
| 仓库内对 `ui-f3m8/admin`、`ui-f3m8/lab`、`static/admin`、`static/lab` 的引用 | 除 `docs/update/`、`docs/kb/`、`docs/archive/` 的历史记录外已清零 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 有人收藏了 `/ui-f3m8/admin/`、`/ui-f3m8/lab/` | 打开得 404 | 通知改用 `/ui-f3m8/console/` |
| `docs/kb/` 里仍描述旧页面 | KB 与代码不一致 | 下次 `/kb-merge` 时随本批记录融合 |
| 控制台全程只在本机假后端上验过 | 真实环境的播放 / 送标 / 离线作业未走查 | 合入前在 dev 环境逐页走查一遍 |
