# routers 层通用能力收进 app/routers/utils/，删除 services/traceback 包

> **变更状态**：已完成（2026-09-28）——纯搬迁与改名，不改运行逻辑
> **知识库**：待沉淀

## 概述

`app/routers/_runs.py` → `app/routers/utils/runs.py`，并入 `media._resolve_run`（→ `resolve_media_run`）与
`admin._no_run`（→ `no_run`）；`app/services/traceback/media_token.py` → `app/routers/utils/media_token.py`，
`app/services/traceback/` 包删除。新建标记型 `app/routers/utils/__init__.py`。对外 HTTP 契约不变。

## 变更背景

- **现状**：routers 层共用的 run 解析以下划线文件 `_runs.py` 放在包根；另有两个同类 helper 私有地写在 `media.py` / `admin.py` 里。`media_token` 挂在 `services/traceback/` 下，但只有 `routers/{media,traceback}` 用它，`services/traceback` 包除它之外已空。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 2 波：每层通用能力落到本层 `utils/`，不用下划线前缀；只有 routers 用的能力不留在 services。

## 方案详情

### 全景：两处搬迁 + 两个 helper 并入 + 一个包删除

```text
app/routers/_runs.py                     → app/routers/utils/runs.py
  + app/routers/media.py::_resolve_run   →   resolve_media_run
  + app/routers/admin.py::_no_run        →   no_run
app/services/traceback/media_token.py    → app/routers/utils/media_token.py
app/services/traceback/__init__.py         删除（只 re-export media_token 三个名字）
                                           app/routers/utils/__init__.py（新建，纯 docstring、零 re-export）
tests/test_traceback_media_token.py      → tests/test_media_token.py
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| run 解析三函数 | `app/routers/utils/runs.py` | §1 |
| 引用改写 | `app/routers/{admin,ai,lab,media,traceback}.py`、`tests/` | §2 |
| 文档与门禁注释 | `README.md`、`app/services/utils/__init__.py`、`tests/test_import_hygiene.py` | §3 |

### 1. `runs.py` 三个函数不合并

| 函数 | 来源 | run 不在时 |
|------|------|-----------|
| `resolve_run(task_id, step_id, run_id)` | `_runs.resolve_run` | 点名 → `NotFoundError`（结构化错误体）；缺省 → 返回 None |
| `no_run(task_id, step_id)` | `admin._no_run` | 构造 `NotFoundError`，给 `resolve_run` 的 None 分支用 |
| `resolve_media_run(payload)` | `media._resolve_run` | 点名 / 缺省一律 `HTTPException(404, "Media file not found")` |

`resolve_media_run` 与 `resolve_run` 响应形态不同（`HTTPException` 的 `{"detail": ...}` vs `NotFoundError` 的结构化错误体，且缺省分支一个 404、一个返回 None），合并会改 `/media/*` 的响应，故保留两个；`no_run` 是 `resolve_run` 的补充而非重复，原样搬入。`resolve_media_run` 取 `MediaTokenPayload`，从同包 `.media_token` 相对导入。

### 2. 引用改写（§8：`app.routers.utils` 是 `app.routers` 的后代，router 里一律相对）

- `admin.py`：`from .utils.runs import no_run, resolve_run`
- `ai.py` / `lab.py`：`from .utils.runs import resolve_run`
- `traceback.py`：`from .utils.media_token import MediaToken` + `from .utils.runs import resolve_run`
- `media.py`：`from .utils.media_token import MediaToken, MediaTokenError` + `from .utils.runs import resolve_media_run`；不再直接 import `app.storage.runs` / `RunIdentity`
- `tests/test_media_token.py`、`tests/test_traceback_router.py`：`app.services.traceback.media_token` → `app.routers.utils.media_token`

`settings.media_token_secret` / `media_token_ttl` 字段名不变。

### 3. 文档与门禁

- `README.md` 项目结构：删 `services/traceback/` 行，`routers/` 下加 `utils/`。
- `app/services/utils/__init__.py` 边界声明与 `tests/test_import_hygiene.py` 注释里拿 `traceback` 举例的「兄弟 service 包」改为 `recording`；`LAYER_PACKAGES` 注释里 storage 读侧列表去掉 `traceback`。
- 门禁表 `BUDGET` / `LAYER_PACKAGES` / `SINGLETONS` 不涉及这几个模块，无需改；`test_services_do_not_import_routers` 保持绿（services 里无人 import media_token）。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 843 passed, 8 skipped |
| 残留检查 `git grep` `_runs import`、`services.traceback`、`services/traceback`、`_resolve_run`、`_no_run` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `docs/kb/`（`SERVICE_TRACEBACK_MEDIA.md`、`ARCHITECTURE_PACKAGE_LAYERS.md` 等）仍写 `services/traceback/media_token.py`、`routers/_runs.py` | 读 KB 时路径过时 | KB 融合时按本篇改名 |
| `docs/DEVELOPMENT.md` §3 的 service 列举仍含 `traceback` | 列举过时（非代码路径） | 与 persistence → alarm 改名一并修 |
