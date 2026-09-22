# 推理产物写侧接线：落盘编排交给 recording，删掉 `inference/feature/` 子包

> **变更状态**：生效中（2026-09-22）　<!-- 在线写侧、离线读侧、facts 调用点同批切到 app/storage/inference；旧子包已删除 -->
> **知识库**：待沉淀
>
> <!-- 承接 20260921 的两步（domain/fact.py + storage/inference 子包），本批是它们的价值兑现 -->

## 概述

在线特征落盘从 `inference/feature/store.py` 的长命单例改成「cq 缓存队列 → recording 第二条
任务队列 → `app.storage.inference`」；离线读侧与 facts 调用点同批切走。产物落位从平铺
`{step}/features.jsonl` 变成 `{step}/inference/`，`app/services/inference/feature/` 整个删除。

## 变更背景

### 现状 / 痛点

`app/storage/inference/`（2026-09-21 落地）与 `app/domain/fact.py` 都已就位但**零生产调用点**。
生产写侧仍是 `feature/store.py`，它一个模块里塞了四样东西：

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | 批缓冲 + 落盘**同步发生在推理写回线程**（`InferCollector`）上 | 攒够 64 条那一次 35 KB 的 write 卡住写回，连带 `slide_window` / `latest_inference` 一起停 |
| #2 | owner fence 注册表 `_owner: Dict[key, cq]` | 与 recording 已有的代次校验是同一条判据的两种表达，两份实现 |
| #3 | `open_fresh` / `close` / `flush` 三套生命周期协议 | 派生出三条跨服务顺序约束（见下），全靠注释互守 |
| #4 | 自己拼 `{base}/{task}/{step}/features.jsonl` 平铺路径 | 绕过数据层的定位收口；`facts.jsonl` 同理 |

### 承接

本批建立在同分支的两步之上：`app/domain/fact.py`（事实契约升格、`producer` 归一、`ts` 必填）
与 `app/storage/inference/`（6 个成员 + codec）。两者当时都刻意不碰调用点。

## 方案详情

### 全景：写回线程不碰盘，落盘全部由 recording 拉走

```text
推理写回线程（InferCollector）                                零 IO
  cq.push_detection(feature)          消费：时序算子滑窗（已有）
  cq.append_ca_features(feature)      缓存：等 sweeper 拉走          ← 新增

[RecordingSweeper 每 1s]  collect_from(cq)
  ① 拉 raw 整段  ② 拉 processed 整段  ③ 断流残帧  ④ 拉 features     ← ④ 新增
       │                                              │
       ├─ submit_segment ─▶ hls 队列 ─────▶ 代次校验 → 首写自清 hls.delete → hls.insert_segment
       └─ submit_features ▶ features 队列 ▶ 代次校验 → 首写自清 inference.delete → inference.append_features

拆除  RunController.stop_run ③ 的 flush_residual(cq) 末尾一并 drain features
停机  main.py 里 recording.lifespan 嵌在 inference 外层，队列比写者活得久（原有结构，未改）
```

| 步骤 / 部件 | 落在哪 | 详见 |
|------------|--------|------|
| cq 缓存队列 `ca_features` | `app/services/client/queues.py` | §1 |
| 第二条任务队列 + 特征写任务体 | `app/services/recording/service.py` | §2 |
| 写回口改为入缓冲；manager 五处删除 | `app/services/inference/` | §3 |
| 离线读侧 + facts 调用点 | `app/services/inference/offline/` | §4 |
| 删旧两型与子包 | `app/services/inference/types.py`、`feature/` | §5 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| A（采用）落盘请求交给 recording，**拉**模式 | recording 多一条队列与一张代次表 | 编排归已有的编排层；inference 侧零编排代码，且**不产生 `inference → recording` 的依赖边**（写回口只碰 cq） |
| B 交给 recording，但**推**模式 | 新增 `inference → recording` 依赖，逐帧入队 | 否：破坏 recording「运行期 CQ 的 drain 者只有 sweeper 一个」的架势 |
| C inference 自建一条队列 | 不动 recording | 否：在不做落盘编排的包里长出第二套落盘编排 |
| D 去掉缓冲，写回线程直接 append | 零组件 | 否：dispatcher 每 tick 每 client 只取一帧，一个 batch 里同 cq 恰好 1 帧，等于逐帧 open+write（实测 94.7 µs/帧）压在写回线程上 |

**为什么两条队列而不是一条**：段写里有 ffmpeg 转码（实测单段 0.26–3 s），features 排在它后面
会跟着一起被背压丢，丢一次是一个 tick 的特征、且静默。代价是代次表必须拆成两张（见 §2）。

**缓存为什么放 cq 而不是 detection 侧**：features 的生产点确实跨客户端，但 `ca_processed` 的
生产者同样是跨客户端的可视化共享池（`visualization/worker.py` 的 `append_ca_processed`）——
三条产物同形：外部共享生产者 → per-cq 缓存 → sweeper 拉走。缓存留在 detection 侧就要在那里
维护一张按 cq 分桶的表 + 锁 + 死亡清理，那正是要删掉的 `_JsonlBuffer._buffers`。

### 1. `app/services/client/queues.py` — 第三条 CA 队列

新增 `ca_features: Deque[FrameFeature]`（容量复用 `ca_maxlen`，900 条 ≈ 60 s @15fps ≈ 2 MB）、
`frames_dropped_features`，以及 `append_ca_features` / `drain_ca_features` 一对。

- **锁复用 `_slide_window_lock`**，不加第 7 把：写者同为写回线程、同一帧连写两样，与
  `_viz_lock` 同护 `ca_processed` + `_latest_rendered` 是同一个理由。全清顺序不变。
- **不能拿 `_slide_window` 当缓冲**：它按算子感受野裁剪，`window_seconds` 调小就静默丢特征。
  两者是同一份数据的两个去处（消费 / 落盘），用例 `test_slide_window_and_buffer_are_independent`
  钉住。
- `drain_ca_features` **没有 `until_ts` 栅栏**，与 `drain_ca_raw` 的不对称是有意的：栅栏挡的是
  「一段视频横跨断流 gap 被 `effective_fps` 反推成慢放」，features 每帧一行、行间无依赖。

### 2. `app/services/recording/service.py` — 第二条队列

逐条照抄段写那一套，不发明新形状：`_FeatureJob` / `submit_features` / `_write_features`
（代次校验 ① → 首写自清 ② → `inference.append_features`）/ `collect_from` 第 ④ 步 /
`flush_residual` 末尾 drain。

两处结构性改动：

```text
self._queue      → self._hls_queue + self._feature_queue      两条，各 config.queue_size
self._claimed_by → self._claimed_hls + self._claimed_features  一域一张表
```

**第四条不变式**（写进模块 docstring）：两张代次表各自只被自己那条队列的线程碰，谁也不读
对方。这是全服务零锁的前提——合并成一张就必须加锁。`forget_task` 因此往两条队列**各排一个**
清理任务（`forget:` / `forget-feat:`），一条队列清不了另一条的表。

首写自清清的是 `{step}/inference/` 整域，**连 `facts.jsonl` 一起带走**：新一代的特征序列变了，
上一代对它的离线分析结果就是脏数据。`{step}/hls/` 一个字节不碰，反之亦然。

### 3. 写侧调用点：inference 侧净删

- `detection/service.py`：`_write_back_results` 里那 5 行换成 `cq.append_ca_features(feature)`；
  `feature_store` 构造参数删除。
- `manager.py`：`self.feature_store` 声明与构造、注入 DetectionService、`start_workflow` 的
  `open_fresh` 块、`stop_workflow` 的 `close` 块、`stop()` 的 `flush` 块——五处全删。
- `run_control.py`：start 侧现在是**零存储钩子**（原有两个 eager 清理都已换成懒惰首写自清）。

**三条跨服务顺序约束随缓冲一起消失**：

| 原约束 | 原先写在哪 | 现在 |
|--------|-----------|------|
| 「排空在途 **先于** flush」 | `DetectionService.stop` 与 `InferenceManager.stop` 的顺序耦合 | 没有在途缓冲，改由 `main.py` 既有的 lifespan 嵌套承接 |
| `close(owner=cq)` 按身份核对才清 owner 记录 | `_JsonlBuffer.close` | 没有 owner 表 |
| 停机全量 `flush()` 兜底未 drain 的分区 | `InferenceManager.stop` | 没有"未 drain" |

### 4. 离线读侧 + facts 调用点

`OfflineRunner` 去掉 `base_dir` 构造参数（存储根归 `settings`），改调数据层：

```text
FeatureStore.load          → inference.read_features
FactLedger.replace_segments → _replace_own_segments：read_facts → 丢掉自己这个 producer 的旧分段 → write_facts
自己拼路径写调试 JSON       → inference.write_debug_result（顺带落进域目录，不再躺在 step 根下）
```

合并规则留在 runner 而不是数据层：`write_facts` 是整体替换，**盲写会吃掉别的 producer 的分段
与所有 `EventFact`**，而「保留谁」是 producer 语义、不是格式事实。

`SegmentFact` 的构造与校验从 `source=` 改 `producer=`（`offline/impl/clean.py`、`mock.py`、
`segmenter.py` 接口约束、`runner._validate`）；`_validate_and_stamp` 改名 `_validate` 并删掉
`meta["producer"]` 盖章与冲突校验——producer 已是一等字段，策略填错名字当场报出来，不替它补。
`cli.py` 的 `query` 改读 `read_facts` + `dataclasses.asdict`，`--source` 参数改名 `--producer`。

### 5. 删旧两型与子包

`inference/types.py` 只剩传输对象两段（`DetectionTask` / `FrameInference`），离线事实那一整节
删除；`app/services/inference/feature/` 整个删除。

### 6. 保留项（不改动）

- `main.py` 的 lifespan 嵌套顺序（`recording` 在 `inference` 外层）本来就对，一行没动。
- `_pending_flush` 那套断流栅栏仍只服务 HLS——features 不需要，这是它比段写简单的地方。
- `persistence` 包内的旧 HLS 四件套仍在仓库里、仍无调用点（本批只把 `start_run` 的 docstring
  标为已无调用点）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 落盘位置 | `{step}/features.jsonl` / `{step}/facts.jsonl` 平铺 + `offline_inference_result.json` 躺 step 根 | 三份都在 `{step}/inference/` |
| 落盘发生在 | 推理写回线程（攒 64 条一次 35 KB write） | recording 的 features 队列线程（每 tick 一次，一批 ~15 条） |
| supersede | run 起始 eager 截断（`open_fresh`） | 懒惰首写自清（新 run 没写出东西就保留上一代） |
| 代次隔离 | owner 注册表（features）+ `_claimed_by`（HLS），两份 | 两张同构的代次表，一套判据 |
| 崩溃丢数据 | 缓冲里 ≤63 帧 | cq 里 ≤1 s + 队列在途 |
| `inference/feature/` | 394 行，4 个类 | 删除 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_recording_service.py` | 61 passed（新增 features 两组 19 条，含真队列 + 真 `storage.inference` 的端到端两条） |
| `tests/test_client_features_buffer.py` | 5 passed（新建：写门 / 满丢最旧 / 原子排空 / close 释放 / 与滑窗独立） |
| `tests/test_offline_pipeline.py` | 32 passed（`TestLoad` 与 `TestReplaceSegments` 的存储层部分迁走，替换语义改测 runner 的 read-modify-write） |
| `tests/test_import_hygiene.py` | 31 passed（recording 新增对 `app.storage.inference` 的依赖在白名单内） |
| 全量 `pytest tests/` | **865 passed**（基线 841，净 +24：新增 24 条、删 8 条重复、迁 8 条） |

删除的测试：`test_feature_store_owner_fence.py`（4 条，被测对象没了，语义由 recording 的
`TestFeatureGeneration` 承接）、`test_offline_reservation.py`（4 条，与 `test_storage_inference.py`
逐条重复）、`test_cq_immutable_run.py::test_open_fresh_supersedes_storage_partition`。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **端到端未跑** | 全部验证止于单测；真实起流下的落盘、重启 supersede、离线回读未验 | 需人确认后在 dev 环境跑（起任务看 `database/{task}/{step}/inference/`、重启同 step、跑 `offline.cli run`） |
| 盘上老的平铺 `{step}/features.jsonl` 立刻不可见 | dev 机器上已有的离线调试数据读不到 | 按既定政策随 TTL 消失；急用时手工挪进 `{step}/inference/` |
| 进程直接停机（非 `stop_run`）时 cq 里 ≤1 s 的特征没人拉 | 每个 step 尾部少 ≤15 帧 | 接受，与 HLS 残段同口径 |
| 两条队列后 `tasks.delete_step` 只能在一条队列里串行 | 跨域删除与另一条队列的在途写可能乱序 | 现状无人调 `delete_step`（TTL 直接 rmtree），真要接线时需两条队列各设栅栏 |
| features 队列满时的 warning 量 | 磁盘持续故障下每 tick 一条 | 当前每 client 每秒至多一条，未加去重；真出现再收 |

## 合入 dev 后的订正（2026-09-22）

dev 的导入规范（[20260922_IMPORT_CONVENTION](20260922_IMPORT_CONVENTION.md)：包内相对、跨包绝对）
合进来后，本批三份记录里的新增模块只有 `app/storage/inference/_temporal.py` 一处违规
（`from app.storage.inference import _jsonl, _layout` → `from . import _jsonl, _layout`），已改。
合并后全量 **866 passed**（本批基线 865 + dev 侧网关新增 1）。

**验收后清残留（2026-09-22）**：`InferenceManager` 的 `db_dir` 构造参数与 `_db_dir` 字段是
FeatureStore 的遗骸（只剩一句 `mkdir`、无人读），删除；`manager.stop_workflow` /
`infer_proxy` / `stage_factory` / `app/domain/fact.py` 里指向 `feature_store`、`open_fresh`、
`SegmentFact.source`、「旧型仍在 types.py」的注释全部改掉；`manager.stop` 的停机注释改为如实
写「最后不到 1 s 的特征能否被拉走取决于时序，已接受」。两份 0921 记录头部状态改为生效中。
