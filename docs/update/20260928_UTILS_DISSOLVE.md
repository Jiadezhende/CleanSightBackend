# 解散 app/utils：gateway 提到 app 根，队列 / 自愈 / 压力 / 指标下沉 services/utils

> **变更状态**：已完成（2026-09-28）——纯搬迁与改名，不改运行逻辑
> **知识库**：待沉淀

## 概述

`app/utils/` 里 5 个模块按使用范围迁出：`gateway.py` 提到 `app/` 根（后端与 `mediamtx_gateway` 进程共用）；
`task_queue` / `worker_guard` / `pressure` / `metrics` 下沉到 `app/services/utils/`。`decorators.py` / `executor.py`
暂留 `app/utils/`，由后续任务删除。引用、门禁表、文档同步改名。

## 变更背景

- **现状**：`app/utils/` 是个跨层杂物包，但这 4 个模块的消费者全在 services 层（加上 `app/main.py` 取指标），`gateway` 则是跨进程共用。
- **承接**：建立在 `app/domain` → `app/types` 改名之上（异常已先一步迁入 `app/types/exceptions.py`）；本篇是 `app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 1 波的第 2 步。

## 方案详情

### 全景：五处搬迁 + 引用面改名

```text
app/utils/gateway.py       → app/gateway.py
app/utils/task_queue.py    → app/services/utils/task_queue.py
app/utils/worker_guard.py  → app/services/utils/worker_guard.py
app/utils/pressure.py      → app/services/utils/pressure.py
app/utils/metrics.py       → app/services/utils/metrics.py
app/utils/{decorators,executor}.py  原地保留
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 引用改名 | `app/`、`mediamtx_gateway/`、`tests/`、`README.md` | §1 |
| `app.utils` 门面收缩 | `app/utils/__init__.py` | §2 |
| 导入门禁表与包 docstring | `tests/test_import_hygiene.py`、`app/services/utils/__init__.py` | §3 |

### 1. 引用面

- `app.utils.gateway` → `app.gateway`（`mediamtx_gateway/{main,rtsp_proxy}.py`、`tests/test_gateway.py` 含 `patch("app.gateway.time.monotonic")`、`tests/test_mediamtx_gateway.py`）。
- `app.utils.{task_queue,worker_guard,pressure,metrics}` → `app.services.utils.*`（client / stream / inference / persistence / recording 各服务及对应测试；`tests/conftest.py` 的 `from app.utils import task_queue` → `from app.services.utils import task_queue`）。
- `app/main.py`：`.utils.gateway` → `.gateway`，`.utils.metrics` → `.services.utils.metrics`（包内相对）。
- `app/gateway.py` 函数体内的 `from app.settings import settings` → `from .settings import settings`：文件升到 `app` 包根后，`app.settings` 成了本包后代，§8 要求写相对。
- `app/utils/executor.py` 函数体内 `from .metrics import` → `from app.services.utils.metrics import`（跨包绝对）。
- `app/storage/**` 只有 `hls/_write.py` 的 docstring 提到 `SerialTaskQueue` 的路径，改了文字；**无代码级依赖**，storage 白名单不受影响。
- 文档：`README.md` 目录树加 `gateway.py` 与 `services/utils/` 两行，`utils/` 行收缩为「GuardedExecutor / 日志装饰器」；异常处理一节 `worker_guard` 路径改名。

### 2. `app/utils/__init__.py` 只剩 `log_call` / `GuardedExecutor` / `ExecutionPolicy`

`SerialTaskQueue` / `guarded_run` 的 re-export 去掉（仓内无人经门面取这两个）；docstring 成员清单同步。

### 3. 导入门禁表与 `services/utils` 包 docstring

- `BUDGET` 新增 4 行：`task_queue` / `worker_guard` / `pressure` 零重依赖 0.20s（stdlib only，实测 ~0.02s）；`metrics` 零重依赖 0.40s（拉 `prometheus_client`，实测 ~0.09s）。四者均不拉 torch / cv2 / ultralytics。
- `LAYER_PACKAGES["app/services/utils"]` 白名单删去 `app.utils`（搬完后包内已无模块依赖它）；注释「无状态纯函数」→「通用能力」。
- `app/services/utils/__init__.py`：边界表删 `app.utils`；成员清单补齐 6 个模块；「与 `app/utils/` 的分工」一段已不成立，换成提案的落点判据一句；「不许有模块级状态」改为「模块级状态只有 metrics 的 Prometheus 指标」——如实反映 `metrics.py` 进包后的事实。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `import mediamtx_gateway.main / rtsp_proxy` | 正常 |
| 全量 `pytest tests/` | 842 passed, 8 skipped（+4 为新登记的 BUDGET 条目） |
| 残留检查 `git grep app.utils.{gateway,task_queue,worker_guard,pressure,metrics}` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `app/utils/` 仍剩 `decorators.py` / `executor.py` | 包未删 | 第 2 波：`log_call` 删除、告警重试就地写进 alarm_worker 后删包 |
| `docs/kb/` 仍写旧路径 | 读 KB 时路径过时 | KB 融合时按本篇改名 |
