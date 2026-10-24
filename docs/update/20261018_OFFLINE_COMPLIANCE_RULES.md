# CLEAN 离线规则层：逐帧概率 → 合规判定 → 告警领域模型落 run 级 alarms.jsonl

> **变更状态**：生效中（2026-10-18）；规则阈值为初值，未在标注数据上标定
> **知识库**：待沉淀

## 概述

离线包里新增与动作分割并列的规则判定模块 `app/services/inference/offline/compliance/`：离线分割 completed 后，离线 CLI 接着调它读 `label_probs.npz` 与
`detections.jsonl`，按 `stages."2".compliance` 的五条规则判「短刷时长不足 / 长刷次数不足 / 长刷早于短刷 / 漏灌注 /
灌注后等待不足」，违规疑似产出 `app.types.alarm.Alarm` 整体写 run 级 `alarms.jsonl`。不接 alarm 服务与数据库；离线仍手动提交。

## 变更背景

- **现状**：CLEAN 离线分割只产出分段与逐帧概率供回看，没有合规判定。
- **需求**：把动作顺序、次数、时长、可见性转成疑似违规告警，提示人回看录像（分诊用途：判成「没做」多一次回看，判成「做了」漏掉违规）。
- **约束**（人已拍板）：
  - 顺序 / 次数 / 时长只在识别层之后核对；分割模型与解码不含这些先验，规则结果不回灌识别层。
  - 实验阶段：告警只落盘为领域模型，重跑整体替换；不入库；离线保持手动提交。
  - 一次 run 即一次完整清洗流程，核对只看单个 run。
  - `water_injection` 未训练、不推理，规则不引用；CLEAN 流程不含漂洗。

## 方案详情

### 全景

```text
admin 手动提交离线作业（不变）
  └ 子进程 cli run
      1  OfflineRunner.run → temporal.jsonl + label_probs.npz（不变；结果新增 run_id = 实际锁定的 run）
      2  completed 才执行 offline.compliance.checker.check_run(同一 run, stage.compliance, stage=stage.alias)
           2a parse_config(规则表 dict)                缺省 / rules 为空 → skipped
           2b read_label_probs / read_detections      无 label_probs → skipped
           2c build_evidence → Evidence                覆盖区间、逐帧可见性、组概率、strict / lenient 两套实例
           2d 每条规则 evaluate → Verdict              compliant / suspected / undetermined
           2e suspected → Alarm → write_alarms         整体替换 alarms.jsonl
      3  stdout 末行 JSON 增 alarm_count；核对异常只进 message，不改分割 status / 退出码

cli check --task-id T --step-id S [--run-id R] [--dry-run]    只重跑 2；--dry-run 逐行打印每条 Verdict、不写盘
```

硬约束：

- 2 只读盘上产物，不碰分割策略对象、不 import torch：改规则只需 `cli check`，不重跑 DINOv2。
- 离线包内两个功能模块互不 import，只在 `offline/cli.py` 串联：`offline/compliance` 只许依赖 `app.types` / `app.storage`
  （门禁 `LAYER_PACKAGES`）；分割侧（segmenter / runner / materials / service / impl）不许 import compliance
  （门禁 `test_offline_segmentation_does_not_import_compliance`）。规则表写在 stage 配置里，由 CLI 取出以 dict 传入。
- `alarms.jsonl` 缺失 = 没核对过；空文件 = 核对过、无告警。skipped 与核对异常都**不碰**已有文件（同 Runner「跳过不覆盖旧事实」；
  存储层不出删除成员）。
- 配置非法、规则引用的类不在模型类表里 → `ValueError`，`cli check` 退出码 1，`cli run` 记进 message。

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 规则表 | [`config/inference_config.yaml`](../../config/inference_config.yaml) `stages."2".compliance`；`StageConfig.compliance` | §1 |
| 规则表解析 | [`compliance/config.py`](../../app/services/inference/offline/compliance/config.py) | §1 |
| 证据层 | [`compliance/evidence.py`](../../app/services/inference/offline/compliance/evidence.py) | §2 |
| 规则层 | [`compliance/rules.py`](../../app/services/inference/offline/compliance/rules.py) | §3 |
| 编排 + 告警映射 | [`compliance/checker.py`](../../app/services/inference/offline/compliance/checker.py) | §4 |
| 告警落盘 | [`storage/inference/_alarm.py`](../../app/storage/inference/_alarm.py)（`read_alarms` / `write_alarms`）；`AlarmMetric` 新增 `STEP_*` 五值 | §5 |
| CLI / 作业服务 | [`offline/cli.py`](../../app/services/inference/offline/cli.py)；`OfflineJob.alarm_count`；[admin API 文档](../api/admin.md) | §6 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| A 规则读逐帧概率：存在性用概率积分、顺序用累计证据分位时刻、时长 / 次数 / 等待用规则层自切的实例 | 依赖策略产 `label_probs`；切分参数归规则层，与策略 pp20 无关 | 采用 |
| B 规则读 `TemporalSegment`（argmax + pp20） | 一个误检碎片就改段数、改首段起点；pp20 会把短 withdraw 并进相邻 insert | 否 |
| C 接 `alarm_service` 入库 | 动单例引用面；入库不可撤回，实验期规则与模型都在变 | 否，入库留待后续 |
| D 判定独立成包（`services/compliance` 或与 online / offline 平级），由上层 service 调度 | 分割完成后要靠钩子 / 回调把判定接上，调度散到两处 | 否；放 offline 包内、与分割互不 import，由离线 CLI 顺序串联 |
| E 每个阶段一套规则实现（同分割策略包） | 五种规则类型与阶段无关，阶段差异全在规则表数据 | 否；判定用五种类型表达不了时再往 `RULE_TYPES` 加类型 |

### 1. 规则表（`stages."2".compliance`）

规则来源：现场 SOP（2026-10-18 人工确认）+《软式内镜清洗消毒实践操作指南》（WS507-2016 蓝本）「胃肠镜手工清洗 · 3.清洗」作语义对照。
类别 ↔ 步骤：`short_brush_cleaning` = 短毛刷刷活塞口 / 钳子管道入口；`long_brush_insert` / `withdraw` = 长毛刷刷管道；`flush` = 灌注清洗液。

| 规则 | 依据 | 类型 | 判定量 |
|------|------|------|--------|
| `clean.short_brush.duration` | 短毛刷激活至少 10 s | duration | 短刷实例内激活时长之和 |
| `clean.long_brush.count` | 长毛刷至少 4 次，一次 = 一次完整插入加拔出 | count（round_trip） | 往返次数 |
| `clean.brush.order` | 长毛刷在短毛刷之后 | order | 短刷证据 90% 时刻 − 长刷证据 10% 时刻 |
| `clean.flush.missing` | 管道灌注清洗液 | presence | flush 证据积分 |
| `clean.flush.wait` | 灌注结束到刷洗约 15 s | wait | 每次灌注结束到其后首个短刷 / 长刷起点 |

未收：灌注 → 长刷顺序（现场「一般如此」，非硬要求）；冲副送水管道（`water_injection` 未训练）；两端见刷头（`brush_tip_out`
检测类低频，没检到 ≠ 没伸出）；擦洗外表面 / 浸没 / 排液充气（无对应类别）。

每条规则带 `id`、`type`、`message`（`str.format` 模板，占位限本类型的判定量与 `at`，解析期校验）、`level`（缺省 `warning`）；
字段多余、组未定义、id 重复都在解析期报错。

### 2. 证据层

- **覆盖区间**：首末帧 detection ts；超出 `LabelProbs.ts` 的部分（截断）没有概率帧，自然计为不可见。
- **可见性**：概率帧 ±`visible_window_s` 内有 `visible_classes` 任一检出（降级帧不落盘，断流处自然不可见）。
- **逐帧时长**：相邻 ts 差，上限 `gap_s`。
- **组概率**：组内各类概率之和，按 `smooth_s` 居中平滑。
- **证据积分**：`Σ p·dt·visible·[p ≥ p_floor]`。不设 `p_floor` 时，600 s idle 上 0.05 的底噪能积出 30 s 假证据。
- **实例**：滞回切分（`≥ p_on` 进、`< p_off` 出），间隔 `< merge_gap_s` 合并，短于 `min_len_s` 丢弃；strict（阈值高、丢得多）
  与 lenient 各一份。激活时长只计激活帧，合并跨过的空档不计，故 strict ≤ lenient。
- **往返**：`insert ∪ withdraw` 的实例帧上按两组概率大小定方向，短于 `min_len_s` 的方向段丢弃、同向相邻段合并，
  「insert 段后接 withdraw 段」计一次；往返之间有没有 idle 都能计，插入中途的短暂方向误判不多计。

### 3. 规则判定（三态）

| type | compliant | suspected | 其余 → undetermined |
|------|-----------|-----------|--------------------|
| presence | 证据 ≥ `present_s` | 证据 ≤ `absent_s` 且整段可见率达标 | 死区；可见率不足 |
| duration | strict 时长 ≥ `min_s` | lenient 时长 < `min_s` 且可见率达标 | 两套跨过阈值；可见率不足 |
| count | strict 计数 ≥ `min_count` | lenient 计数 < `min_count` 且可见率达标 | 同上 |
| order | `d ≤ 0` | `d > margin_s` | `0 < d ≤ margin_s`；任一组证据 < `present_s` |
| wait | 每次灌注按 lenient 实例算的间隔（结束晚、开始早，偏短）都 ≥ `min_s` | 有一次按 strict 实例算的间隔（偏长）仍 < `min_s` 且区间可见率达标 | 其余；灌注后没有刷洗 |

两套实例夹逼：两套都判违规才报 suspected、都判合规才算 compliant，不让单一切分参数决定告警。count 为 0、duration 为 0 即漏刷，不另设 presence。

### 4. 告警映射

只映射 suspected：`alarm_type` = 流程违规；`metric` 按规则类型取 `STEP_MISSING` / `STEP_DURATION` / `STEP_COUNT` / `STEP_ORDER` /
`STEP_WAIT`（与阶段无关，阶段看 `stage`）；`mode` = `SETTLEMENT`；`stage` = stage 别名；`seq` = run 内序号；`timestamp` = 违规时刻（order 取长刷证据 10% 时刻，wait 取违规那次的刷洗起点；presence / duration / count
是整段判定、没有可定位的时刻，取覆盖末端）；`metadata` = `rule_id`、`rule_type`、`at_ms`（可回看定位的违规时刻，整段判定为 null）、`producer`（`temporal.jsonl`
里分段的产出者）、覆盖区间与判定量（时刻为 `*_at_ms` int，浮点保留 3 位）。`{at}` 渲染为相对覆盖起点的 mm:ss。

### 5. `alarms.jsonl`

`{run}/inference/alarms.jsonl`，一条 `Alarm` 一行，路线 C 整体替换；枚举存 value，`timestamp` 落 `timestamp_ms`（int）。坏行、未知枚举值跳过 + warning。

### 6. CLI 与作业服务

- `cli run` 末行 JSON：`{status, producer, segment_count, alarm_count, message}`；未核对 `alarm_count` 为 null。
- `cli check` 末行 JSON：`{status, run_id, alarm_count, message}`；run 不存在、配置非法退出码 1。
- `OfflineJob.alarm_count` 从末行 JSON 读，经 admin 作业接口透出（`docs/api/admin.md` 已同步）。

### 7. 保留项

- 分割策略包、`temporal.jsonl` 格式、`alarm_service` / `alarm_sink` / 在线告警链路不动。
- admin 页面未加告警列与告警读取端点。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| CLEAN 离线结果 | 只有分段与逐帧概率 | 另有按五条规则核出的违规疑似告警（run 级 `alarms.jsonl`） |
| 改规则的成本 | — | 改 YAML + `cli check`，不重跑模型 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_compliance.py`（合成概率 + 检测） | 碎段 vs 连续段证据一致、底噪不积分、遮挡 / 截断降可见率；4 次往返（有无间隙、插入中夹方向误判）计 4；五条规则合规 / 违规 / 死区各态；解析期报错；核对落盘、skipped 不碰旧文件、dry-run 不写盘；`cli check`；仓库 YAML 可解析 |
| `tests/test_storage_inference.py::TestAlarms` | 往返到毫秒、整体替换、缺失 vs 空、坏行隔离、编码失败不碰盘、run 已回收不重建 |
| CLI / 作业服务 | `cli run` 末行 JSON 含 `alarm_count`；核对失败仍 completed、退出码 0；`OfflineJob.alarm_count` 解析 |
| 导入门禁 | `_alarm` 与 compliance 各模块登记零重依赖；compliance 进 `LAYER_PACKAGES`（在独立包位置时临时加 import inference 验证过门禁会红）；新增分割侧不得 import compliance 的门禁 |
| 全量 `pytest tests/` | 1169 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 阈值未标定，真实 run 上没跑过 | 误报 / 漏报率未知 | 在有人工标注的 run 上 `cli check --dry-run` 对照；违规注入（删段 / 换序 / 缩短）测检出 |
| insert / withdraw 互混让往返多计 | 多计 = 漏报次数不足（危险方向） | strict 方向段 `min_len_s` 兜；标定时专看多计率 |
| 核对异常时旧 `alarms.jsonl` 保留 | 重跑分割成功但核对失败时，告警与新分段不配套 | 看 `cli run` / 作业 message；修好后 `cli check` |
| 依赖策略产 `label_probs` | 不产概率的策略无法核对（skipped） | 有需要时以分段 p=1 兜一条证据来源 |
| 入库 | `AlarmReportTask.from_dict` 写死入队时刻，入库时违规时刻会丢 | 接 DB 时改 `from_dict` 尊重传入时间 |
| 规则表写错要到离线作业运行时才报 | 作业 message 里才看到 | 仓库 YAML 已有解析用例兜；可加作业服务启动预检（同物料预检，只告警） |
