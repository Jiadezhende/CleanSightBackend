# admin / lab 接口去掉 `-f3m8` 混淆后缀：`/admin-f3m8` → `/admin`，`/lab-f3m8` → `/lab`

> **变更状态**：生效中（2026-10-25）
> **知识库**：待沉淀

## 概述

两组后台接口的路由前缀去掉混淆串：`/admin-f3m8/*` → `/admin/*`，`/lab-f3m8/*` → `/lab/*`，路径其余部分与契约不变。控制台、测试、`docs/api/`、README 同步改。网关宽松档改为 `/admin/`（带尾斜杠）。静态页挂载 `/ui-f3m8` 不动。

## 变更背景

- **现状**：两组前缀带 `-f3m8` 混淆串，意在防扫描器命中。
- **判断（人已拍板，2026-10-25）**：混淆不是访问控制——控制台 JS 里写着全部路径；盲扫命中率去掉后缀后本来就低（裸 `/admin`、`/lab` 无路由，目录爆破在 404 处停止递归；同 IP 300s 内 10 次 404 即封 1h；生产关了 `/openapi.json`）；而更敏感、本就无后缀的 `/api/terminate`、`/traceback/*` 也没靠它。后缀只增加记忆和书写成本，去掉。真正的访问控制（后台接口 IP 白名单 / 鉴权）另行决定。

## 方案详情

### 全景

```text
app/routers/admin.py  APIRouter(prefix="/admin")      ← 原 /admin-f3m8
app/routers/lab.py    APIRouter(prefix="/lab")        ← 原 /lab-f3m8
gateway 宽松档默认值   …,/admin/,…                     ← 原 /admin-f3m8；带尾斜杠
调用方                 app/static/console/、tests/、docs/api/、README、integration_tests/README
```

### 一次切换而不是新旧并存

两组接口的调用方全在本仓库（控制台与测试），旧 admin / lab 页已在本分支删除，没有仓库外调用方需要过渡；改动是机械的前缀替换，替换后全仓 grep 无残留、全量测试通过。故不做双前缀并存。

### 宽松档带尾斜杠

网关前缀按 `startswith` 匹配。写成 `/admin` 会把扫描器最常探的 `/admin`、`/admin.php`、`/administrator/...` 一起划进宽松档，不计反扫描；写成 `/admin/` 只放行控制台真正调用的 `/admin/<端点>`。`/lab/*` 与之前一样走普通档。

### 保留项（不改动）

- `/ui-f3m8` 静态挂载（控制台地址 `/ui-f3m8/console/`）。
- 所有端点的请求 / 响应契约。
- `docs/update/`、`docs/kb/`、`docs/design/` 里的历史写法。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 1231 passed |
| `tests/test_gateway.py` 新增：`/admin`、`/admin.php`、`/administrator/index.php` 在生产默认前缀下仍走普通档（第 3 次 429） | 通过 |
| 全仓 grep `admin-f3m8` / `lab-f3m8` | 除历史记录外无残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 网关启用（`CLEANSIGHT_GATEWAY_ENABLED=true`）且部署机 `.env` 自己写了 `CLEANSIGHT_GATEWAY_RELAXED_PREFIXES`（含旧的 `/admin-f3m8`）时；生产目前网关未启用（人告知，2026-10-25），不受影响 | 升级后 `/admin/` 不在宽松档，控制台运维页约 60 次/分的轮询撞普通档 60/60s，429 后还可能升级封禁 | 部署时检查该变量，有则把 `/admin-f3m8` 换成 `/admin/` 并追加 `/ui-f3m8` |
| 有人在脚本 / 收藏里直接调 `/admin-f3m8/*`、`/lab-f3m8/*` | 404 | 通知改新路径 |
| 后台接口仍无鉴权 | 见 `PUT /lab/config` 可改 LS 地址导致 token 外泄等 | 待人选定方案（后台接口单独 IP 白名单 / 鉴权 / 仅内网） |
