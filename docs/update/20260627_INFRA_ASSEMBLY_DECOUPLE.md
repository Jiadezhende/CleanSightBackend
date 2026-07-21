# 基础装配层解耦：存储根单一真源 + 消除跨服务穿透

> **变更状态**：已完成（2026-06-27）　<!-- 两批改造均已落地并通过验证：全量 pytest 206 passed -->
> **知识库**：已沉淀 → [kb/ARCHITECTURE_API_SURFACE.md](../kb/ARCHITECTURE_API_SURFACE.md)(2026-07-21)
>
> 相关：[20260620_LAYERED_INFER_DATAFLOW.md](20260620_LAYERED_INFER_DATAFLOW.md)（分层数据流核实）、[20260626_THREAD_INSTANCE_LIFECYCLE_AUDIT.md](20260626_THREAD_INSTANCE_LIFECYCLE_AUDIT.md)（上一轮生命周期审计）。

## 概述

- **改了什么**：把持久化存储根目录 `storage_base_dir` 上移为 `settings.py` 的单一真源，persistence / inference / traceback 三方一律**读**它，删除 `InferenceManager` 反向 push `db_dir` 进 `PersistenceManager` 的跨服务穿透代码。
- **为什么改**：基础装配层审计（见下）发现「离框架越近的装配越糟」。最脆的一处是 [`InferenceManager.__init__`](../../app/services/inference/core/manager.py) 伸手改 `persistence_manager.hls_pool.strategy.db_dir` 私有字段——穿透封装，persistence 一重构 worker 结构，inference 静默崩。
- **影响面**：`app/settings.py`、`app/services/persistence/config.py`、`config/persistence_config.yaml`、`app/services/inference/core/manager.py`、`app/services/traceback/segment_finder.py`。运行时行为不变（当前 `_db_dir` 与 persistence 默认根本就解析到同一路径 `<root>/database`，穿透代码实为 no-op）。

### 审计阻断项

| 编号 | 问题 | 风险 | 本次处理 |
|------|------|------|---------|
| P1 | `InferenceManager` 伸手改 `persistence_manager` 私有 `hls_pool.strategy.db_dir`；且只补了 strategy，**漏补 `_cleanup_worker.db_dir`** | 重构静默炸 / 清理线程扫错目录（潜在） | 本批修复 |
| P1' | `InferenceManager._db_dir` 用 `__file__` 自数 5 层重算存储根，又把结果 push 给 persistence——既重算又反向写，责任方向颠倒 | 双源易漂 | 本批修复 |
| P2 | `_STEP_TO_STAGE = {"1":"LEAK","2":"CLEAN"}` 写死在核心类，泄了 workflow 层的「配置驱动」 | 新增阶段必改源码 | 第二批落地（见下） |
| P3 | `InferenceManager(ca_maxlen=500)` 是**死参数**：设了从不读，真正队列长度由 `client/config.py ← inference_config.yaml(ca_maxlen)` 决定 | 误导性死参数冒充配置入口 | 第二批落地（见下） |

> **P3 澄清**：审计原文把"600 vs 500 静默降级"当成一个 bug，实为**两个不同的 `ca_maxlen` 被混淆**：
> `InferenceConfig.ca_maxlen`（[config.py](../../app/services/inference/config.py) 默认 600，**真实生效**，经 `client/config.py` 驱动队列长度，yaml 实配 2700）
> 与 `InferenceManager.ca_maxlen`（默认 500，**死参数**，赋值后从不读）。不存在"降级"，只有一个该删的死参数。

## 改动详情

### 1. `app/settings.py` — 新增 `storage_dir` 字段 + `storage_base_dir` 解析属性（单一真源）

存储根本质是部署级路径，和已有的 `model_path` 同类，理应由全局 `settings` 拥有；它卡在 `persistence_config.yaml` 里才是异常。新增：

```python
# env: CLEANSIGHT_STORAGE_DIR
storage_dir: str = "./database"

@property
def storage_base_dir(self) -> Path:
    """相对路径以项目根为基，避免读写两侧因 cwd 不同而分叉。"""
    p = Path(self.storage_dir)
    return p.resolve() if p.is_absolute() else (Path(__file__).parent.parent / p).resolve()
```

> 相对路径解析逻辑从原 [`PersistenceConfig.storage_base_dir`](../../app/services/persistence/config.py) 原样搬移，保证读写两侧对相对路径的解析完全一致。

### 2. `app/services/persistence/config.py` — `StorageConfig` 去掉 `base_dir`，属性改为委托 settings

- `StorageConfig` 删除 `base_dir` 字段（`enable_cleanup` / `cleanup_days` 等持久化自有参数保留）。
- `from_dict` 对 storage 段按 `__dataclass_fields__` 过滤未知键，防御性兼容仍残留 `base_dir` 的旧 yaml（静默忽略而非崩）。
- `storage_base_dir` 属性体改为 `return settings.storage_base_dir`。
- `_log_loaded_config` 中 `self.storage.base_dir` → `self.storage_base_dir`。

### 3. `config/persistence_config.yaml` — 删除 `storage.base_dir` 行

存储根改由 `CLEANSIGHT_STORAGE_DIR` / `settings` 决定，yaml 不再持有，彻底单源。

### 4. `app/services/inference/core/manager.py` — 读真源 + 删穿透

#### 旧
```python
base_dir = Path(__file__).parent.parent.parent.parent.parent.resolve()
self._db_dir = Path(db_dir) if db_dir else base_dir / "database"
...
_persistence_manager.config.storage.base_dir = str(self._db_dir)
_persistence_manager.hls_pool.strategy.db_dir = self._db_dir   # 伸手进私有
self.persistence_manager = _persistence_manager
```

#### 新
```python
self._db_dir = Path(db_dir) if db_dir else settings.storage_base_dir
...
# persistence 自己从 settings.storage_base_dir 读根（与此处同源），不再反向 push
self.persistence_manager = _persistence_manager
```

### 5. `app/services/traceback/segment_finder.py` — `get_default_base_dir()` 直读 settings

委托链从 `→ persistence config → ...` 收敛为直接 `settings.storage_base_dir`，去掉对 persistence config 对象的依赖。

### 6. 删除死配置链 `enable_db_write`（写 file_path 表）

file_path 表早已不写。核查发现整条开关链 gate 不到任何代码——属性赋值后全无读取，worker 还硬编码 `False`：

| 站点 | 状态 | 处理 |
|------|------|------|
| `config/persistence_config.yaml: storage.enable_db_write` | 翻它无效 | 删 |
| `StorageConfig.enable_db_write` 字段 | 无 gate | 删 |
| `PersistenceConfig.enable_db_write` 属性 | 无调用方 | 删 |
| `HLSPersistenceStrategy.__init__(enable_db_write=...)` + `self.enable_db_write` | 只赋值从不读 | 删 |
| `hls_worker.py: HLSPersistenceStrategy(enable_db_write=False)` | 硬编码,唯一构造点 | 删 kwarg |

> `persist_segment` 内部根本没有写 file_path 表的分支，该能力此前已移除，此次只是清掉残留的死配置与死参数线。

### 7. 保留项（不改动）

- persistence 的 `enable_cleanup` / `cleanup_days` / `cleanup_interval_seconds` 等**仍留 persistence_config.yaml**——这些是持久化自有职责，不上移。
- `InferenceManager.__init__(db_dir=...)` 显式入参保留，作测试/特殊场景覆盖；仅默认值从「自数 5 层」改为「读 settings」。

## 数据通道 / 行为说明

| 值 | 写/拥有 | 读 | 本次影响 |
|------|--------|----|---------|
| `storage_base_dir` | `settings`（单源） | persistence(HLS/cleanup)、inference(FeatureStore/FactLedger/db_dir)、traceback | 来源收敛，运行时路径不变 |

## 第二批改动方案（P2 + P3，已完成）

> 与第一批同支 `refact/infra`，单独成一次提交。

### 8. P2 — stage 主键改用 step_id，`alias` 承载可读名（消除 `_STEP_TO_STAGE` 映射）

**设计决策（取代"映射外移"方案）**：不再维护任何 `step → stage` 映射，而是让 stage 的**主键直接是 step_id**，
路由退化为恒等；人类可读名（"LEAK"）下沉为每个 stage 的 `alias` 字段。

| 维度 | 取值 | 用途 |
|------|------|------|
| **主键**（yaml key / `cq.set_stage` / stage_configs key / 路由 / 分发 / 持久化） | `step_id`（`"1"`/`"2"`/`"MOCK"`） | 一切功能性标识，**零转化直接用** |
| **alias**（yaml 内字段） | `"LEAK"`/`"CLEAN"`/`"MOCK"` | **仅可读性**：写告警 `step_name` + 可视化叠字 |

> 关键原则（用户定调）：功能性 id 全程用 step_id，不做多余转化；alias 不落任何状态（ClientQueues **不**存第二份），
> 是 config 的纯函数，只在可读性出口按需查一次。沿用既有 `data_models._set_task_metric_map` 的全局 map 先例。

#### `stage` 消费点分类（决定每处用主键还是 alias）

| 消费点 | 用途 | 用 |
|--------|------|----|
| dispatcher / manager:268 / visualization:166（`stage_configs.get(stage)`） | 配置查找、分发 | 主键 |
| temporal 实时告警（`persist_alarm`+`AlarmRecord`，×2） | 写告警 | **alias** |
| manager `_persist_settlement_alarms` | 写告警 | **alias** |
| `FixedVisualizer.render` → `_draw_global_info(stage)` | 帧上叠字 | **alias** |
| `ClientQueues.get_frontend_message` | — | **死代码（无调用方）**，不处理 |

#### 改动文件

| 文件 | 改动 |
|------|------|
| `config/inference_config.yaml` | `stages:` 主键 `LEAK→"1"`、`CLEAN→"2"`（`MOCK` 保留）；每 stage 加 `alias: LEAK/CLEAN/MOCK` |
| `config/client_config.yaml` | **删** `state.initial_stage`（死配置：读了只打日志，从未传进 `create_client`） |
| `app/services/client/config.py` | **删** `state.initial_stage` 字段 + 引用它的 debug 日志行 |
| `app/services/client/queues.py` | `initial_stage` 默认值 `"LEAK"` → `"MOCK"`（**行为修正**：taskless 客户端从"跑 LEAK 检测"改为 MOCK 透传） |
| `app/services/inference/config.py` | `StageConfig` 解析 `self.alias = cfg.get("alias", stage_name)` |
| `app/services/inference/stage_factory.py` | 新增 `build_stage_alias_map() -> {step_id: alias}` |
| `app/services/inference/data_models.py` | 仿 `task_metric_map` 加 `_set_stage_alias_map` / `get_stage_alias(key)`（未命中回退 key 本身） |
| `app/services/inference/core/manager.py` | 删类常量 `_STEP_TO_STAGE`；`set_task` 改恒等路由 `stage = current_step if current_step in stage_configs else "MOCK"`；`start()` 处灌 alias map（紧邻 `_set_task_metric_map`） |
| `app/services/inference/workers/temporal.py` | 告警两处 `self._stage` → `get_stage_alias(self._stage)` |
| `app/services/inference/workers/visualization.py` | 传 `get_stage_alias(stage)` 给 `fixed_visualizer.render` 显示（配置查找仍用主键 `stage`） |

> 注：MOCK 仍是未知 step 的兜底键，`alias: MOCK`；其余功能性 dict 的键随 yaml 主键自动翻成 `"1"/"2"`，
> 因为 `_get_stage_configs()` 由 `config.list_stages()` 派生，无需逐处改硬编码字面量（代码里 `"LEAK"`/`"CLEAN"` 仅存于 docstring 示例）。

### 9. P3 — 删除 `InferenceManager` 死参数 `ca_maxlen`

`InferenceManager.__init__(ca_maxlen=500)` 设 `self._ca_maxlen` 后全包再无读取（grep 证实），
且 [`ai.py`](../../app/services/ai.py) 构造时根本没传它。真实队列长度由 `client/config.py ← inference_config.yaml`
决定，与此参数无关。

| 文件 | 改动 |
|------|------|
| `app/services/inference/core/manager.py` | 删 `__init__` 的 `ca_maxlen` 形参与 `self._ca_maxlen = max(50, ca_maxlen)` 行 |
| `app/services/ai.py` | 构造处确认无 `ca_maxlen` 传参（本就没有），无需改动 |

> 保留 `InferenceConfig.ca_maxlen`（真实生效，勿删）。仅删 InferenceManager 上的同名死参数。

## 验证

### 第一批（存储根 + enable_db_write）

| 项 | 结果 |
|----|------|
| 三源一致性 smoke（settings / persistence / traceback 解析到同一绝对路径） | ✅ `<root>/database`，三方相等 |
| 自定义根贯穿 persistence 全部消费者（strategy + cleanup） | ✅ |
| `enable_db_write` 死链清除（grep 归零） | ✅ |

### 第二批（P2 stage 主键 + P3 死参）

| 项 | 结果 |
|----|------|
| stage 主键 smoke：`list_stages() == ['1','2','MOCK']`，alias map `{1:LEAK,2:CLEAN,MOCK:MOCK}`，未知键回退自身 | ✅ |
| 恒等路由单测（`test_inference_stage_routing`：已配 step→恒等、未配→MOCK） | ✅ 5 passed |
| taskless 默认 MOCK 透传 + `clear()` 重置 + 别名解析 smoke | ✅ |
| `_STEP_TO_STAGE` / `initial_stage`(配置) / `InferenceManager.ca_maxlen` 残留 grep | ✅ 归零 |
| 全量 `pytest tests/` | **206 passed** |

> 行为变更（有意）：未分配任务的客户端默认 stage 由 `LEAK` → `MOCK`，从"跑泄漏检测"改为纯透传。
> 死配置清理：`client_config.yaml: state.initial_stage` 此前只被读取打印、从未传入 `create_client`，一并删除。
