# 2026-07-14 离线推理链路开发与自检记录

## 当前基线

- 工作目录：`F:\暑期实习\CleanSightBackend_feat_offline_direct`
- 分支：`feat/offline-infer`
- 基线提交：`origin/feat/offline-infer` 的 `6892faf`
- 本次处理方式：从远端 `feat/offline-infer` 直接拉取干净工作区，再迁移离线 baseline 和文档。

## 本次开发目标

一期目标是把离线时序推理链路跑通到可手动验证的状态：

```text
features.jsonl
  -> 读取完整检测序列
  -> 通用离线时序模型接口
  -> 结构化 timeline / SegmentFact
  -> facts.jsonl
```

当前实现不做自动调度、不做复杂服务化，只保证可以单独启动 worker，读取指定任务的 `features.jsonl`，产出有效行为时间线，并可选写入 FactLedger。

## 开发内容

### 1. 离线时序模型通用接口

新增 `app/services/inference/offline/interfaces.py`，定义离线模型运行时的输入输出结构：

- `OfflineFrame`：单个时间点的一组多 source 检测框。
- `OfflineFeatureSequence`：完整时序输入，包含 `task_id`、`step_id`、帧序列、source 列表和 fps。
- `FramePrediction`：逐帧预测结果。
- `TimelineSegment`：合并后的行为时间段。
- `OfflineInferenceResult`：一次离线推理的完整结构化结果。
- `OfflineTemporalModel`：模型统一接口，后续真实 MS-TCN / ASFormer / BiRNN 类模型可以按这个接口接入。

### 2. 规则式 baseline segmenter

新增 `app/services/inference/offline/segmenter/`，作为当前可运行 baseline：

- `features.py`：把 `OfflineFeatureSequence` 转成 `[T, 62]` 多维特征。
- `brush_rule.py`：用规则逻辑完成逐帧动作分类。
- `__init__.py`：提供 `create_segmenter()` 注册入口。

当前 62 维特征包含：

```text
9 类目标 * 5 个目标级特征 = 45
7 组目标关系 * 2 个关系特征 = 14
时间位置特征 = 3
总计 = 62
```

当前规则主要覆盖：

```text
short_brush_cleaning
long_brush_insert
flush
air_injection
idle
```

`long_brush_withdraw` 暂未强行输出，因为仅靠单帧检测框很难稳定区分插入和拔出，后续应交给真实时序模型或引入方向/轨迹特征。

### 3. 接入已有 OfflineSegmenter 主链路

新增 `app/services/inference/offline/segmenters/brush_rule.py`，把规则模型包装成已有主链路可用的 `OfflineSegmenter`：

```text
FeatureStore.load_many(...)
  -> OfflineRunner
  -> BrushRuleSegmenter.preprocess(...)
  -> BrushRuleSegmenter.segment(...)
  -> SegmentFact
  -> FactLedger.replace_segments(...)
```

这条链路与图中的 `FeatureStore -> OfflineAnalyzer -> SegmentFact -> FactLedger` 基本一致。当前没有单独 `OfflineJudge` 类，timeline 以 `SegmentFact` 形式落盘；后续如果要做合规判定、告警复算，可以在 `SegmentFact` 之后补离线 Judge。

### 4. 手动 worker 调试入口

新增 `app/services/inference/offline/worker.py`，用于不启动完整后端时做离线推理验证。

运行：

```powershell
.\.venv\Scripts\python.exe -m app.services.inference.offline.worker run `
  --task-id 90001 `
  --step-id 2 `
  --storage-base-dir database `
  --source clean_large `
  --source clean_small `
  --fps 10 `
  --min-duration-s 0.15 `
  --model brush_rule `
  --write-ledger `
  --output-json database\90001\2\offline_inference_result.json
```

查询：

```powershell
.\.venv\Scripts\python.exe -m app.services.inference.offline.worker query `
  --task-id 90001 `
  --step-id 2 `
  --storage-base-dir database
```

输入文件：

```text
{storage_base_dir}/{task_id}/{step_id}/features.jsonl
```

输出文件：

```text
{storage_base_dir}/{task_id}/{step_id}/offline_inference_result.json
{storage_base_dir}/{task_id}/{step_id}/facts.jsonl
```

### 5. Windows JSONL BOM 兼容

手动 worker 回环时发现 Windows PowerShell `Set-Content -Encoding UTF8` 可能写入 UTF-8 BOM，导致 `json.loads()` 首行报：

```text
Unexpected UTF-8 BOM
```

已将 worker 读取 `features.jsonl` 的编码改为 `utf-8-sig`，兼容手工写入的 BOM 文件，同时不影响 FeatureStore 正常写出的无 BOM JSONL。

## 测试方式与结果

### 1. 全量单测

命令：

```powershell
$env:CLEANSIGHT_ENV = "test"
$env:CLEANSIGHT_DB_HOST = "127.0.0.1"
$env:CLEANSIGHT_DB_PORT = "5432"
$env:CLEANSIGHT_DB_NAME = "test"
$env:CLEANSIGHT_DB_USER = "test"
$env:CLEANSIGHT_DB_PASSWORD = "test"
$env:CLEANSIGHT_ALARM_REPORT_URL = "http://127.0.0.1/mock"
$env:CLEANSIGHT_GATEWAY_ENABLED = "true"
$env:TMP = (Resolve-Path '.').Path
$env:TEMP = (Resolve-Path '.').Path
.\.venv\Scripts\python.exe -m pytest tests/ -q -p no:cacheprovider --basetemp=tmp_pytest_tests_final
```

结果：

```text
324 passed in 39.60s
```

说明：

- 本次提交不修改 gateway 或部署脚本。
- 全量测试使用隔离的 `CLEANSIGHT_ENV=test` 和 dummy DB/API 环境变量，避免读取本机 `.env.dev`。
- 如果直接用本机 `.env.dev` 且其中 `CLEANSIGHT_GATEWAY_ENABLED=false`，既有 `tests/test_gateway.py` 会失败；这是本地开发配置影响测试前置条件，不是本次离线链路回归。本次按你的要求不提交 gateway 测试夹具修复。

### 2. 离线链路专项测试

命令：

```powershell
$env:TMP = (Resolve-Path '.').Path
$env:TEMP = (Resolve-Path '.').Path
.\.venv\Scripts\python.exe -m pytest `
  tests\test_offline_inference_worker.py `
  tests\test_offline_pipeline.py `
  tests\test_offline_reservation.py `
  -q -p no:cacheprovider --basetemp=tmp_pytest_offline2
```

结果：

```text
35 passed in 2.18s
```

覆盖内容：

- `FeatureStore.load_many()` 多 source 读取。
- `FactLedger.replace_segments()` 幂等替换。
- `OfflineRunner` completed / skipped / 异常不写入。
- `OfflineSegmenter.preprocess()` 预处理接缝。
- `offline.worker` 读取 `features.jsonl`、生成 timeline、写 `facts.jsonl`。
- Windows UTF-8 BOM JSONL 读取兼容。

### 3. Worker 命令行回环

手动构造 `tmp_offline_worker_manual/90001/2/features.jsonl`，运行：

```text
features.jsonl
  -> offline.worker run
  -> offline_inference_result.json
  -> facts.jsonl
  -> offline.worker query
```

结果输出的 timeline：

```json
[
  {
    "label": "short_brush_cleaning",
    "start": 0.1,
    "end": 0.4,
    "confidence": 1.0
  },
  {
    "label": "air_injection",
    "start": 0.4,
    "end": 0.6,
    "confidence": 1.0
  }
]
```

结论：手动 worker 可完整完成读取、推理、落盘和查询。

### 4. 开发库只读连通性

命令只执行 `select 1`，不写表：

```powershell
.\.venv\Scripts\python.exe - <<检查数据库连接脚本>>
```

结果：

```text
db_select_1= 1
```

说明：`.env.dev` 中数据库配置可以建立连接；本次没有对业务表做建表、删表或写入操作。

## 当前边界

- 规则 baseline 只是工程链路验证，不代表最终模型精度。
- 当前可输出 `short_brush_cleaning`、`long_brush_insert`、`flush`、`air_injection`、`idle`，但长刷拔出还需要更强时序信息。
- 自动调度、任务结束自动触发、离线 Judge、结果入业务库、离线复算告警仍未实现。
- 生产配置 `config/inference_config.yaml` 的 `offline` 未默认启用，避免影响在线链路。

## PR 前注意事项

- `.env.dev` 已被 `.gitignore` 忽略，不应进入提交。
- `wheelhouse_win/`、`.venv/`、`database/`、`logs/`、临时 pytest 目录不应进入提交。
- 本次提交只包含离线链路代码、离线链路测试和文档；业务账号配置、gateway 测试夹具和安装脚本修改先不提交。
- 不直接 push `main` / `dev`，建议从最新 `dev` 切个人分支，例如：

```powershell
git checkout dev
git pull origin dev
git checkout -b ywc/feat/offline-infer-baseline
```

当前工作区已有改动时，不要盲目切分支；应先确认文件清单，再决定是继续当前分支整理提交，还是迁移到个人分支。
