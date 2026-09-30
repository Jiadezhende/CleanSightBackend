> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 检测标准

本文件记录当前代码实际执行的检测标准、阈值与可调位置。调标准优先改 `config/inference_config.yaml` 或对应 Operator，并补测试。服务内部实现见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)。

## 只有配置过的 step 能跑，未配置按参数错误处理

stage 主键 = step_id 字符串，`resolve_stage` 恒等路由、无兜底 stage。当前只有 `"1"`（LEAK）与 `"2"`（CLEAN）。

- `/api/start`：`current_step` 非数字、YAML 未定义、或定义了但无 detector → 400，在动旧 run 之前判，旧 run 不受影响。
- 离线提交（admin 作业 / CLI）：step 未定义或 `offline` 为空 → 400 / CLI 退出码 1。
- 运行时推理失败不切 stage：逐帧降级为空结果（画面照常、没框），降级帧不写入 `detections.jsonl`。

## LEAK（stage "1"）：气泡实时告警 + 弯折结算告警

| 检测点 | Detector（流） | 规则 | 告警 | 阈值（YAML `params`） |
|--------|----------------|------|------|------------------------|
| 漏气 | `BubbleDetector`（`bubble`，`bubble-best.pt`） | `bubble_leak` → `BubbleOperator`，`realtime: true` | 实时，`high`，metric `BUBBLE` | `window_seconds: 3.0`、`birth_rate_threshold: 0.5` |
| 弯折 | `BendingDetector`（`bending`，`bend-best.pt`） | `bending_check` → `BendingOperator`，`realtime: false` | 结算，`warning`，metric `BENDING` | `debounce_frames: 5`、`required_bend_actions: 4` |

两个 detector 都是 `conf_threshold: 0.1`、`iou_threshold: 0.45`。

### 漏气：新气泡出生率超阈值即告警，上升沿触发

```text
每个新帧 → ByteTrack 跟踪 → 本帧新出现的 track_id 数 new_count（与 seen_ids 比对）
birth_rate = 窗口内 new_count 之和 / 窗口内帧数          （窗口 = window_seconds）
birth_rate > birth_rate_threshold 且未锁存 → 告警「持续产生新气泡…疑似漏气」，锁存
回落到阈值以下 → 解除锁存；超阈值期间叠字显示 birth_rate，正常时无叠字
```

- 无结算告警，`finalize()` 返回空。
- ByteTrack 参数（`track_high_thresh` 等）与 `frame_rate=10` 写死在 `online/temporal/impl/bubble.py::_BYTETRACK_ARGS`，不在 YAML。

### 弯折：累计 STRAIGHT→BENT 次数不足则结算告警

```text
每个新帧：有 class_name == "bent" 的框？
  STRAIGHT 下连续 debounce_frames 帧 bent → 切 BENT，bend_actions += 1
  BENT 下连续 debounce_frames 帧无 bent → 切回 STRAIGHT
实时：bend_actions > 0 时叠字「弯曲动作 N/required」，不上报告警
结算：bend_actions < required_bend_actions → warning「弯曲动作不足」
```

结算在 run 拆除时触发一次：`/api/terminate`、换 step 或 URL 重启、健康检查清理、进程停机都经 `stop_run`。

### 阈值以「帧」计，改检测帧率会改变标准的含义

`birth_rate` 的分母是窗口内帧数，`debounce_frames` 是连续帧数，二者都随检测帧率 `inference_fps`（`app/settings.py` 的 `raw_fps / inference_decimation`）变化。调 `inference_decimation` 后须重新评估这两个阈值，代码不会报错。

## CLEAN（stage "2"）：只识别动作，不判合规、不产告警

两个 detector 按目标尺寸分组，`conf_threshold: 0.25`：

- `clean_large`（`CleanLargeDetector`，`clean-large-best.pt`）：手、scope_control_body、scope_mid_section。
- `clean_small`（`CleanSmallDetector`，`clean-small-best.pt`）：syringe、air_gun、scope_distal_end。

### 在线动作识别 `clean_monitor`：只出叠字

`CleanOperator`（订阅两流，`realtime: true`）用 `gru-final.pt` 对最近 `window_seconds: 10.0` 的帧窗做动作分类，叠字 `Action: <label>`，不产告警。类别：idle / air_injection / flush / long_brush_insert / long_brush_withdraw。

- `model_input_fps: 7.5` 必须等于训练帧率：配错不报错、静默误分类；高于 `inference_fps` 时每次 `/api/start` 都会失败。
- `objects` 词表数量决定模型输入维度（每物体 6 维），须与训练一致。
- 两个流名不在 `AlarmMetric` 里，`realtime: true` 对 `signals_10s` 实际无效。

### 离线动作分割：手动提交，结果只供回看

默认 `CleanNodepGRUSegmenter`（`clean-offline-gru-nodep.pt`，`model_input_fps: 7.5`、`confidence_override: 1.0`、`min_duration_s: 0.2`）。run 结束后经 admin「运行离线推理」手动提交（自动触发未实现），逐帧分为 6 类（idle / water_injection / flush / long_brush_insert / long_brush_withdraw / short_brush_cleaning），idle 为背景不成段，短于 `min_duration_s` 的段丢弃。结果写 `temporal.jsonl` 与逐帧概率 `label_probs.npz`，不判合规、不产告警、不入库。

可调位置：`stages."2".offline`。换 `class` 须同时换 `params`（备选类 ↔ 权重映射见 YAML 注释）；`model_input_fps` / `confidence_override` 须与训练口径一致（当前取值的训练口径待核验）。

## 试纸比色：独立图片判定，不走 stage 路由

`POST /algorithm/colorstrip`，与 LEAK / CLEAN 无关。同一张图里以瓶身色卡 2000 刻度块为参考下限，试纸 L\* 更低（更深）即合格；待测试纸条数须等于 `spec.expected_strips`（当前 1），否则拒判（`ok=False`，≠ 不合格）。阈值在 `app/services/algorithm/colorstrip/params.yaml`，调档规则与验收工装见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)。

## 告警去重：同一 run 内同 metric 同 mode 5 秒只放行一次

实时与结算告警都经 `ClientQueues.append_alarm_record_with_gate()`：闸门表挂在单个 CQ（即单个 run）上，键为 `metric:mode`，固定 5 秒冷却。编排在 `online/temporal/alarm_sink.persist_alarms`，细节见 [SERVICE_ALARM.md](SERVICE_ALARM.md)。

## 代码来源

- `config/inference_config.yaml`
- `app/services/inference/online/service.py`（`resolve_stage`）、`app/services/inference/config.py`（`require_offline`）
- `app/services/inference/online/detection/impl/{bubble,bending,clean}.py`
- `app/services/inference/online/temporal/impl/{bubble,bending,clean}.py`
- `app/services/inference/offline/impl/clean.py`
- `app/services/inference/stage_factory.py`（`build_task_metric_map`）
- `app/services/inference/online/temporal/alarm_sink.py`、`app/services/client/queues.py`
- `app/services/algorithm/colorstrip/{grader.py,params.yaml}`
- `tests/test_alarm_increment.py`、`tests/test_inference_stage_routing.py`
