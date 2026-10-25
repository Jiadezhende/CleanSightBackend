# 控制台第 1 批：外壳 + 任务总览上线，任务清单改为「磁盘 + 平台库」并在库不通时自动退回

> **变更状态**：生效中（2026-10-25）
> **知识库**：待沉淀

## 概述

新增控制台 `/ui-f3m8/console/`（无构建，Vue 全局版 + 原生 ES module），本批落地外壳（侧栏 / 面包屑 / 后端健康灯 / toast）、出网层与任务总览页；其余四页暂指向旧 admin / lab 页。
后端配套两处：新增 `GET /admin-f3m8/stages`（step 能力表）；`GET /lab-f3m8/tasks` 的 db 模式改为以磁盘为准、平台库过滤补字段，库不通自动退回仅磁盘并在响应里标明 `source`。
`/ui-f3m8` 进 gateway 宽松档。

## 变更背景

- **现状 / 痛点**：
  - 旧 admin / lab 两页各自选任务、手输 id，前端写死 step 名与业务字段；7 处轮询错误被 `catch {}` 吞掉。
  - `/lab-f3m8/tasks` 的 db 模式按 `clean_task` 分页，会列出没有录像的任务；平台库挂了直接 503，要人手动切 storage。
  - 控制台按 ES module 分文件，开一次页拉十几个静态资产，普通档配额（60/60s）刷新几次就 429。
- **触发来源**：控制台改造提案 [20261025_ADMIN_CONSOLE_REDESIGN_PROPOSAL.md](20261025_ADMIN_CONSOLE_REDESIGN_PROPOSAL.md) 的第 1 批（人已确认页面划分与交互，2026-10-25）；任务清单来源的语义由人给定：「平台库 db 指在磁盘基础上再过滤一次平台库，平台库连不上则切到磁盘 storage」。

## 方案详情

### 全景

```text
浏览器 /ui-f3m8/console/#/tasks
  app.js 外壳 ── loadCatalog() ─▶ GET /admin-f3m8/stages ─▶ step 别名 / 有无离线模型（前端不写死）
           └── startJobs()   ─▶ GET /admin-f3m8/offline/jobs（3s）─▶ 侧栏「分析」角标 + 芯片离线状态
  pages/tasks.js
    在线 ── GET /admin-f3m8/overview（3s）
    全部 ── GET /lab-f3m8/tasks?q&limit&offset
              task_source=db ：磁盘有 raw 段的任务 ∩ 平台库 → 补 IP / 状态 / 时间 → 分页   source="db"
                               平台库抛 DatabaseError ─▶ 退回下一行                       source="storage"
              task_source=storage：只枚举磁盘                                             source="storage"
  api/http.js：所有请求走 request()；轮询连续 3 次失败 → 顶栏健康灯变红并显示原因
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| step 能力表 | `app/routers/admin.py` `list_stages` | §1 |
| 任务清单「磁盘 + 平台库」与退回 | `app/routers/lab.py` `_list_db_tasks` / `list_lab_tasks`；`app/db/tasks.py` `query_tasks` | §2 |
| 静态资产进宽松档 | `app/settings.py` `gateway_relaxed_prefixes` | §3 |
| 控制台外壳、出网层、任务总览 | `app/static/console/` | §4 |

### 1. `GET /admin-f3m8/stages`

读 `load_stage_config()`，每个整数主键的 stage 出 `{step_id, alias, has_offline_model, has_compliance}`，按 step_id 升序。`has_offline_model = offline 段非空`（与 `require_offline` 同判据），`has_compliance = compliance.rules 非空`（与「rules 为空 = 不核对」同判据）。契约见 [docs/api/admin.md](../api/admin.md#get-admin-f3m8stages)。

### 2. `GET /lab-f3m8/tasks`：db 模式以磁盘为准

- 先枚举磁盘上有 raw 段的任务（与 storage 模式同一个 `_list_raw_runs` 判据），再 `db_tasks.query_tasks(ids)` 一次 IN 查询；库里没有的任务不列。
- `q` 在过滤后的行上做子串匹配（task_id / source_ip / status，大小写不敏感），排序 `updated_time desc, task_id desc`，再分页，`total` 按过滤后计。
- `DatabaseError` → WARNING `[Lab] 平台库不可达，任务清单退回仅磁盘` → 走 storage 分支；响应新增 `source: "db" | "storage"`。
- 旧的 `query_task_page`（按库分页）在调用点迁完后单独删除。

### 3. gateway：`/ui-f3m8` 进宽松档

默认 `gateway_relaxed_prefixes` 追加 `/ui-f3m8`。代价：静态路径不计入反扫描，静态目录本身无可枚举的敏感内容。

### 4. 控制台前端（`app/static/console/`）

```text
index.html           加载 vendor/vue.global.prod.js + <script type="module" src="app.js">
app.js               外壳：侧栏 5 项、面包屑、健康灯、toast / 确认框；页面注册表
core/router.js       hash 路由、replaceQuery / replacePath、离开保护
core/ui.js           toast / confirmBox / toastError
core/format.js       时间格式化（入参一律毫秒）
api/http.js          request() / ApiError / createPoller() / 健康灯状态
api/endpoints.js     各端点函数
api/catalog.js       step 能力表与 stageOf()
api/jobs.js          离线作业共享视图（3s 轮询）
api/adapters.js      作业 *_at 秒 → 毫秒，按 run 建索引
pages/tasks.js       任务总览
pages/pending.js     未迁页面的占位（指向旧页面），第 5 批删除
```

- 任务总览：「在线 / 全部」两档 + 搜索；step 芯片只显示别名与离线状态，点芯片进 `#/run/{task}/{step}/{run_id}`（run_id 取自 `run_ids`，整页锁同一个 run），直播中的 step 进 `#/live/{task}`。
- 离线状态取自作业列表（completed 显示段数与违规数，其余状态原样映射）；作业不在内存里时按 `offline_steps` 显示「有结果 / 未跑」。
- `source="storage"` 时工具栏挂提示，IP / 状态列显示「—」；提示文案按 `GET /lab-f3m8/config` 的 `task_source` 区分「库连不上已退回」与「仅磁盘模式」。

### 保留项（不改动）

- 旧 `admin/`、`lab/` 两页原样可用，第 5 批迁完后删除。
- `/lab-f3m8/tasks` 的 storage 模式行为不变。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 平台库挂了 | 任务清单 503，需手动切 storage | 自动退回仅磁盘，`source` 标明，页面挂提示 |
| 没有录像的任务 | db 模式会列出 | 不列 |
| step 名 / 离线能力 | 前端写死 | 来自 `/admin-f3m8/stages` |
| 轮询失败 | 静默 | 顶栏健康灯变红，列出失败的轮询与原因 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_lab_tasks_api.py`（重写：db 模式过滤 / 排序分页 / q / 退回；storage 模式） | 6 passed |
| `tests/test_admin_stages.py`（新增） | 3 passed |
| `tests/test_db_queries.py`（新增 `query_tasks`） | 通过 |
| `tests/test_static_mount.py`（console 页 + 全部 module 以 JavaScript MIME 返回）、`tests/test_gateway.py` | 通过 |
| 全量 `pytest tests/` | 1242 passed |
| 前端 | 本机假后端 + 无头 Edge：在线 / 全部 / 平台库退回提示三种状态渲染正确，无控制台报错；未在 dev 环境连真实后端走查 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| db 模式每次请求都枚举全部任务目录 | 任务目录多时清单变慢（与 storage 模式同量级，TTL 15 天内） | 观察；慢了再加缓存 |
| `.env.test` 自己覆盖了 `CLEANSIGHT_GATEWAY_RELAXED_PREFIXES=/health,/task/message` | test 环境开控制台可能 429（旧 admin 页的 `/admin-f3m8` 轮询在该环境同样不在宽松档） | 需要时在 `.env.test` 追加 `/admin-f3m8,/ui-f3m8` |
| 「回放 / 实时 / 分析 / 运维」四页还是占位 | 控制台暂只能看总览 | 第 2–5 批 |
