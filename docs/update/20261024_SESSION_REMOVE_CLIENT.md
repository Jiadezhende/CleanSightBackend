# 删除 client 包：配置读取平移进 session 包，session 包上导入门禁，开发规范 §3 改为「跨服务运行数据走 topic」

> **变更状态**：生效中（2026-10-24）——session/mq 重构批 8（收尾）
> **知识库**：待沉淀

## 概述

删除 `app/services/client/`（`ClientService`、`ClientQueues`、单例）；`config.py` 原样平移为 `app/services/session/config.py`（`config/client_config.yaml` 不改名），去掉已无调用方的 `cq_kwargs()`。
导入门禁把 session 包登记为白名单分层包。`DEVELOPMENT.md` §2 / §3 / §4 / §8、README、API 文档同步。运行时行为不变。

## 变更背景

- **承接**：批 7（[20261024_SESSION_REGISTRY_SWITCH](20261024_SESSION_REGISTRY_SWITCH.md)）后 `ClientService` / `ClientQueues` 已无调用方，client 包只剩配置读取还在用。
- 提案 T6：删 client 包与 CQ、配置读取平移、导入门禁、§3 改写。按 DEVELOPMENT.md §6，删旧单独成批。

## 方案详情

| 改动 | 落在哪 |
|------|--------|
| 删 `client/__init__.py`、`instance.py`、`service.py`、`queues.py` | `app/services/client/` |
| `client/config.py` → `session/config.py`（`ClientConfig` / `get_client_config` 名字不变，删 `cq_kwargs`） | 调用方：`session/topics.py`、recording、DetectionService、InferenceService、`main.py` 内存自检、测试 |
| 白名单分层包：`app/services/session` 只许 import 自身、`app.services.utils`、`app.types`、`app.settings`；`session/core` 再去掉 `app.settings` 与 topic 声明 / 配置 | `tests/test_import_hygiene.py` 的 `LAYER_PACKAGES`，session 每个模块登记 `BUDGET` |
| §3 改为「跨服务运行数据走 topic」；§2 factories 例子、§4 日志例子、§8 导入例子换成 session | `docs/DEVELOPMENT.md` |
| 目录树、数据流图换成 session / topic | `README.md` |
| `/terminate` 响应 `errors` 前缀 `client_service:` → `session:`（另列 `settlement:` / `frontend:`）；admin 数据源写 `session_service` | `docs/api/api.md`、`docs/api/admin.md` |
| CLAUDE.md「client 中台」→「session 中台」；配置文件注释里的 `ClientQueues` 字样 | `CLAUDE.md`、`config/*.yaml` |

- **门禁的意义**：session 是所有 service 的共同下游。它一旦 import 某个 service，就成了 service → service 依赖的后门，也背上了业务名词；`core` 更不认识 topic 声明与配置。
- **对外可见的一处变化**：`/terminate` 部分失败时 `errors` 里注销注册表失败的前缀从 `client_service:` 变成 `session:`（批 7 起）。这只是诊断串，状态码与其余字段不变。

## 变更效果

| 维度 | 重构前 | 重构后 |
|------|--------|--------|
| 每 run 对象 | CQ：身份 + 状态 + 10 槽位 + 告警 + 埋点，6 把 payload 锁 | `Session`：身份 + 状态 + topic 字典，1 把锁 |
| 推理侧新增或改通道 | 改 client 包 | 改 `inference/online/topics.py` 与读写方 |
| 写门 | 逐方法手写、3 种变体、锁外检查 | channel 统一执行，与状态切换同锁 |
| 每 run 辅助状态 | viz / health_monitor 各建 task_id 表 | topic，随 run 关闭释放（告警账本为显式例外） |
| 锁序 | 隐式 | DEVELOPMENT.md §9 三层约定 + 锁表 |
| 拓扑查询 | 读 CQ + 各 worker | `grep "channel(TOPIC)"` 得全部读写点 |

**自测结果**

| 项 | 结果 |
|----|------|
| 导入门禁（session 白名单、逐模块 BUDGET、单例引用面、相对 / 绝对） | passed |
| `import app.main` | 通过 |
| 全量 `pytest tests/` | 1229 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| KB 仍按 client / CQ 描述（`SERVICE_CLIENT_STATE`、`ARCHITECTURE_DATA_FLOW`、`DESIGN_CONCURRENCY_AND_QUEUES`、`ARCHITECTURE_PACKAGE_LAYERS` 等） | 读 KB 的人会看到旧结构 | 批 1–8 记录均标「待沉淀」，由 `/kb-merge` 融合 |
| 提案「遗留更名事项」：`[PRESSURE]` component `client_queues`、admin `clients[]` / `queue_depths` 的 `ca_*` 键、health 的「客户端」字段、`config/client_config.yaml` | 名字与实现不一致 | 各自另开分支，与前端 / 部署约定后改 |
| dev 环境真流端到端与 15 路压测未跑 | 见批 5a、批 7 记录 | 需人在 dev 环境跑 |
