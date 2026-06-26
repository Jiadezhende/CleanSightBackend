# 基础装配层解耦：存储根单一真源 + 消除跨服务穿透

> **变更状态**：进行中（2026-06-27）　<!-- 当前已完成审计与方案定稿，代码改造分批落地中 -->
> **知识库**：待沉淀
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
| P2 | `_STEP_TO_STAGE = {"1":"LEAK","2":"CLEAN"}` 写死在核心类，泄了 workflow 层的「配置驱动」 | 新增阶段必改源码 | 待办（下一批） |
| P3 | `InferenceManager(ca_maxlen=500)` 是**死参数**：设了从不读，真正队列长度由 `client/config.py ← inference_config.yaml(2700)` 决定。同一语义三个默认值（500 死 / 2700 / 2700） | 误导性死参数冒充配置入口 | 待办（下一批，直接删参数） |

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

## 后续计划

1. **P2**：`_STEP_TO_STAGE` 外移进 `inference_config.yaml`，与 stage 配置驱动体系对齐。
2. **P3**：删除 `InferenceManager` 的死参数 `ca_maxlen`（含 `ai.py` 构造处一并清理）。

## 验证

| 项 | 结果 |
|----|------|
| 三源一致性 smoke（settings / persistence / traceback 解析到同一绝对路径） | ✅ `<root>/database`，三方相等 |
| `InferenceManager` 模块导入（已无反向 push） | ✅ |
| `test_traceback_segment_finder.py`（解析逻辑改测 settings 层） | 16 passed |
| 全量 `pytest tests/` | **206 passed** |

> 顺带修复：[`tests/test_inference_stage_routing.py`](../../tests/test_inference_stage_routing.py) 的 fixture 仍用旧字段名
> `_client_locks`（已于上一轮 [生命周期审计](20260626_THREAD_INSTANCE_LIFECYCLE_AUDIT.md) 单锁化为 `_client_lifecycle_lock`），
> 属预存失败（与本改动无关，stash 本改动后仍 fail 已验证），一并对齐。
