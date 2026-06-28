# 数据模型分层归位：共享契约上浮 domain/、DTO 归边界、帧/告警契约去冗余

> **变更状态**：生效中（2026-06-28）
> **知识库**：待沉淀
>
> 相关：[20260627_INFER_CONTRACT_PURITY.md](20260627_INFER_CONTRACT_PURITY.md)（同支前序：契约提纯，删死字段/metric 显式化）、[20260627_STREAM_OPERATOR_FRAMEWORK.md](20260627_STREAM_OPERATOR_FRAMEWORK.md)（流源/流算子框架，Detector/Operator 命名来源）、[20260627_INFRA_ASSEMBLY_DECOUPLE.md](20260627_INFRA_ASSEMBLY_DECOUPLE.md)（装配层解耦）。

## 概述

- **改了什么**：把散落 5 文件、混装 3 种生命周期（运行时 dataclass / Pydantic DTO / SQLAlchemy ORM）的数据模型按职责归位；并修正 4 处建模缺陷。
- **为什么改**：`app/services/inference/data_models.py` 物理嵌在一个 service 内 3 层深，却被 routers / client / persistence 当全 app 契约中枢反向依赖——"跨 service 伸内脏"的结构倒置。Review 又暴露出 `InferenceRequest` 双 frame 字段冗余、Req/Res 不对称、`Alarm`/`AlarmRecord` 重叠、`CleaningTask` 运行时 VO 却用 Pydantic 等建模缺陷。
- **影响面**：48 文件（净 −176 行）。新增 `app/domain/`、`app/models.py`、`inference/{fact,naming}.py`；删 `app/models/` 包、`data_models.py`、`ProcessedFrame`/`BaseFrame`/`TaskTracebackRequest`/`AlarmRecord`。**运行时行为不变**（删的都是死字段/死类型/纯透传 DTO），211 单测全绿。

### 分层准则（本次确立，三类 × 三处归属）

| 种类 | 职责 | 归属 | 谁构造 |
|------|------|------|--------|
| Pydantic DTO | HTTP/WS 边界（请求体/响应体） | **跟 router 走**（router 内或相邻） | router/边界，core service 不碰 |
| `@dataclass` | 进程内运行时/传输/契约 | 共享契约→`app/domain/`；队列作业→拥有它的 service | 产出方 |
| SQLAlchemy ORM | DB 行 | `app/models.py` | — |

> 「共享契约 → domain」的判据是**别的模块读它的字段**（依赖其形状），仅持有/转发引用不算。

## 改动详情

### 1. `app/domain/`（新增）— 跨服务共享契约上浮

纯 dataclass/enum，零框架依赖、零 service 逻辑，依赖方向单向（domain 不依赖任何 service）：

- `frame.py`：`Frame`（队列载体）
- `detection.py`：`Detection`、`FrameDetections`
- `render.py`：`RenderSpec`、`RenderItem`、`RenderType`
- `alarm.py`：`AlarmType`、`AlarmMetric`、`ALARM_MODE_*`、`Alarm`
- `task.py`：`CleaningTask`

切断 **persistence / client / routers 对 inference 的契约依赖**（原 `from app.services.inference.data_models import ...` → `from app.domain.*`）。

> `Detection` 本身不跨 service（仅 inference 直接 import），但它是 `FrameDetections.detections: List[Detection]` 的元素；`FrameDetections` 已因 client 读其字段（slide_window / signals_10s）上浮 domain，domain 不能反向依赖 service，故 `Detection` 必须随容器同住 domain。

### 2. `app/models.py`（`app/models/` 包收敛为单文件）— 只留 ORM

`DBTask` / `DBAlarm` 移入单文件 `app/models.py`；`app/models/{__init__,frame,task}.py` 删除（git 识别为 `app/models/task.py → app/models.py` rename）。

### 3. `inference/data_models.py` 拆解 → `models.py` + `naming.py` + 删除

- `inference/models.py`：inference 私有**数据结构**统一收此——传输对象（`DetectionTask`/`FrameInference`，online 热路径）+ 离线预留事实契约（`EventFact`/`SegmentFact`/`fact_from_json`，分节标注"online 不产不消费"）。
- `inference/naming.py`（新增）：`get/_set_task_metric_map`、`get/_set_stage_alias` —— YAML 驱动的运行时注册表。**注意它不是数据结构**（含可变全局状态 + 惰性加载逻辑），故不进 models，与被动数据形状分离。
- `data_models.py` 内容分流完毕后删除。

> 取舍：事实契约一度单拆 `fact.py` 做"离线隔离岛"，后并回 `models.py`（按"模块数据结构统一放 models"约定，隔离用分节注释达成）；naming 因是有状态+逻辑的注册表、非数据，坚持独立。

### 4. 建模修正

- **`CleaningTask`：Pydantic → `@dataclass`**。它由 `routers/api.py` kwargs 构造、非请求体解析，是运行时 VO；全仓无 `.dict()`/`.model_dump()`/校验依赖（已 grep 确认），无行为变化。
- **`InferenceRequest` → `DetectionTask`，扁平化**。原 `{client_id, frame:np, timestamp, stage, frame_data:Frame}` 中 `frame`/`timestamp` 与 `frame_data` 重复，且 `frame_data` 全仓无人读（死字段）→ 扁平为 `{client_id, stage, timestamp, frame:np}`。
- **`FrameInference`：`result` → `detections`**，Req/Res 前导字段对齐 `client_id/stage`。
- **`Alarm` 吸收 `AlarmRecord`**（四形态减一）：核心字段由 Operator 产出，`mode/stage/seq/timestamp` 落 alarm_log 时由 [`alarm_sink.persist_alarms`](../../app/services/inference/workflows/alarm_sink.py) 就地补全；删死字段 `count`（全仓无读写）。

### 5. DTO 归边界 — `ProcessedFrame` 消除

WS 端点 `/ai/video` 实测**只发 JPEG data URL**，`ProcessedFrame` 的 `inference_result`/`task_id` 等字段从不上线（死负载）。故：

- 删 `ProcessedFrame`/`BaseFrame`；[`manager.get_result`](../../app/services/inference/core/manager.py) 改为只返回 domain `Frame`，去掉 `as_model` flag、`_create_processed_frame`/`_make_json_serializable` 及按 client+ts 的编码缓存（manager 减负 ~60 行）。
- 编码（`cv2.imencode` → base64）内联到 [`routers/ai.py`](../../app/routers/ai.py) WS 循环边界；去重前置到编码前（按 `frame.timestamp`），省去重复编码。
- 删死类型 `TaskTracebackRequest`（全仓零引用）。

## 保留项（刻意不动）

- **`FactLedger` + `EventFact`/`SegmentFact`**：离线 segmenter 已知集成点的廉价预留，并入 `inference/models.py` 离线分节，不删。
- **`HLSPersistenceTask` / `AlarmPersistenceTask`**：persistence 自有的队列作业（与 `DetectionTask` 对称），留 persistence/，不上浮。
- **`FrameInference` 留 inference**：client 仅以 `TYPE_CHECKING` 持有其引用做原子快照槽，从不读字段；产/消费两端（service 写、visualization 读）都在 inference 内，属管线传输对象，非共享契约。
- **`Frame` 留作队列载体**：ca_raw/ca_processed/ca_ready + latest_rendered 的元素类型，未扁平化（扁平化反而 ~10 处改 tuple）。

## 护栏（落库字符串未动）

枚举 value（`AlarmType`/`AlarmMetric`/`ALARM_MODE_*`）、`AlarmPersistenceTask.from_dict/to_dict` 的 dict key、`EventFact/SegmentFact.to_json` 字段名、`DBTask/DBAlarm` 列名 —— 全部保持不变（仅改 Python 符号，不改 wire/DB 形态）。

## 验证

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 211 passed |
| `import app.main` | OK |
| YAML class 路径解析（`StageFactory.create_detectors_for_stage`） | OK（未触碰 `workflows/` 目录名与 `inference_config.yaml` 的 class 路径） |
| 残留引用扫描（旧位置/旧类型名） | 0（含注释 stale 引用一并清理） |
