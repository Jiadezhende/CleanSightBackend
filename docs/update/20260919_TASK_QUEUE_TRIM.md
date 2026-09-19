# `SerialTaskQueue` 瘦身：注释压掉 1/4，去掉三处不产生作用的设计

> **变更状态**：生效中（2026-09-19）　<!-- 仅 app/utils/task_queue.py + 其单测；无调用点改动 -->
> **知识库**：待沉淀

## 概述

[`app/utils/task_queue.py`](../../app/utils/task_queue.py) 213 → 152 行。注释只保留「加 worker 会
静默破坏正确性」「不许丢的任务要传大 timeout」两条护栏；同时删掉 `guarded_run` 包裹、公共
`qsize()`、`is_running` 三处**在全仓没有任何作用**的设计。唯一的生产消费方
[`recording/service.py`](../../app/services/recording/service.py) 用到的仍只有 `start` / `stop` /
`submit`，一行未改。

## 变更背景

审查这个组件的抽象价值时的结论：它只有一个生产消费方，复用价值≈0，真正在赚的是「类名钉住串行
约束」+「给 recording 提供可替换的测试缝（`InlineQueue`）」。既然价值集中在这两点上，其余的自我
说明与未被使用的接口就是噪声。

## 具体变更

### 1. `guarded_run` 包裹删除，线程直接 `target=self._run`

原来消费线程走 `guarded_run(self._run, ...)`，宣称「主循环崩了会自动重启」。实际不可达：`_execute`
已经吞掉**所有**任务异常，而 `_run` 自身只有 `queue.get` / `get_nowait`，没有会抛的逻辑。留着它的
唯一效果是让人以为这里有自愈。

现在 `_run` 的 docstring 显式写明「不包 `guarded_run`，因为异常在 `_execute` 就被吞掉」——这是个
需要写下理由的决定，其他 worker（`dispatcher` / `temporal.actor` / `visualization.pool`）仍在用
`guarded_run`，那里的主循环确实有会抛的业务逻辑。

`app.utils.worker_guard` 依赖随之去掉，本模块依赖上界变成纯 stdlib。

### 2. 删掉公共 `qsize()` 与 `is_running`

| 成员 | 唯一使用者 | 处理 |
|------|-----------|------|
| `qsize()` | 自己的单测 `test_qsize_reflects_backlog`（`stop()` 内的 warning 直接用 `self._queue.qsize()`） | 删，同批删掉那条只测它自己的用例 |
| `is_running` | 自己的单测 fixture teardown | 删；fixture 改为无条件 `stop()`（未 start / 已 stop 都是 no-op） |

两者都没进过生产代码，docstring 里「供压力观测」的用途从未成立。将来要做队列深度上报再加回来是
3 行的事，那时会有真实的读取方。

### 3. 注释压缩

模块 docstring 42 → 19 行，方法 docstring 平均减半（`__init__` 的整段说明压成建队列那行的行内
注释）。解释性叙述全部删掉，保留的护栏一条没少：

- **「串行」是类名唯一挡得住的东西**：加第二个 worker 不报错，表现是 tfdt 碰撞、旧段串进新 run
- **`submit` 默认 `timeout=1.0` 是按可丢任务定的**，purge 这类必须显式传大 timeout 并检查返回值
- **一条队列一个语义、谁用谁 new、不建全局注册表**（停机顺序约束属于域）
- `stop()` 排空是有意的；`_execute` 异常不出函数

删掉的是：用法示例里过期的 `hls.write_segment`（实际叫 `insert_segment`，
[20260911_STORAGE_HLS_DOMAIN §遗留](20260911_STORAGE_HLS_DOMAIN.md) 记的那条命名不一致就此消除）、
`_execute` 指向 `BOUNDARY_LAYER_EXAMPLES.md`「第 5 个边界层」的单向引用（那份文档只讲 4 个，没提过
本模块）、以及「它不做什么」五行表格（压成一句）。

## 验证

```bash
pytest tests/test_task_queue.py tests/test_recording_service.py tests/test_import_hygiene.py
# 76 passed
```

## 遗留 / 待决

- **组件位置未动**。放在 `app/utils/` 的言外之意是「通用基建，来复用我」，与它自己「一条队列一个
  语义」的语气相反；严格说它是 recording 的私有机制，更诚实的位置是
  `app/services/recording/_queue.py`。不现在搬是因为
  [`persistence/manager.py`](../../app/services/persistence/manager.py) 的 `alarm_queue` 仍是手搓
  `queue.Queue` + `AlarmWorkerPool`，等告警也迁出 legacy 路径就是第二个消费方，搬过去再搬回来是白做。
  若到时告警仍没迁，再收回 recording 内部。
