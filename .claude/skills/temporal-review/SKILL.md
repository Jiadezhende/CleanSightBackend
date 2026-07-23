---
name: temporal-review
description: "Review a temporal analyzer (时序分析模型算子) as it's onboarded into the inference chain — a GRU/Transformer/sequence-model Operator that turns detection windows into actions/states. Use when asked to: review 时序分析器/时序模型/GRU/Transformer 算子接入、review 时序推理代码、时序 Operator review、审查特征适配器/feature adapter、审查入模帧率/重采样/train-serve skew、review temporal analyzer/operator. Checklist is severity-ranked: silent-wrong feature bugs first, then crashes, then lifecycle/causality, then style."
---

# 时序分析器接入 Review

新时序分析器 = 一个 `Operator` 子类（analyze+judge 合一、单 `self._sm`）内嵌序列模型（GRU/Transformer…），把检测框滑窗喂成动作/状态。架构原理见 [workflows/CLAUDE.md](../../../app/services/inference/workflows/CLAUDE.md) 与 [operator.py](../../../app/services/inference/temporal/operator.py) docstring，本 skill 只讲**怎么审**。

按严重度分档：**🔴 静默错 > 🟠 崩溃 > 🟡 生命周期/因果 > ⚪ 契约/放置/风格**。静默错最贵——不报错、结果错，模型背锅。逐档过下列清单，每档发现如实归档，别拿风格问题挤掉静默错。

## 🔴 特征适配器 `_adapt_to_features` — 静默错重灾区

检测框 → 模型输入张量，这里错了不抛异常、只是喂脏数据。逐项质问：

- **class_id 局部→全局映射**：`base = class_id * 4` 是否把**模型局部 id** 当**全局槽位**？上游 `class_id` 是各 YOLO 自己 `result.names` 的原始 id（[detector.py:139](../../../app/services/inference/detection/detector.py)）。多流共写一张特征表时，流 A 的 id=0 与流 B 的 id=0 撞槽 → 必须经 `self.objects` 显式重映射，别赌"两 .pt 共享全局标签"。
- **同类多实例**：`max(..., key=confidence)` 只留 top-1？one-slot-per-class 结构性丢同类多目标 —— 可否接受问场景。
- **缺席 vs 退化**：缺席槽填 `0.0`，但 `(0,0,0,0)` 也是合法框长相 —— 有无 presence 位区分"无此物"与"此物零尺寸"？
- **归一化裁剪**：docstring 常写"归一化到 [0,1]"，但代码有 `clamp` 吗？越界框/坐标系不匹配会喂负值或超界。
- **分辨率来源**：除的是帧自带 `metadata["frame_shape"]`（[detector.py:151](../../../app/services/inference/detection/detector.py)）还是全局死配置 `get_client_config().frame.resize_*`（[config.py:21](../../../app/services/client/config.py)）？后者与真正定尺寸的 decoder ffmpeg `scale`（[decoder.py:86](../../../app/services/stream/decoder.py)）靠默认值巧合对齐，改一边忘同步即整体偏移且不报错。**适配器应是 `(detections, shape) → tensor` 纯函数，别读全局态。**

## 🔴 入模时间密度 — train/serve fps skew（shape-check 抓不到的静默错）

模型按**训练 fps 的时间密度**学时序；实时检测采样率 ≠ 训练 fps 时，不重采样就是喂错密度 —— 不抛异常、只是动作错，模型背锅。

- **为何 shape-check 漏判**：fps 只改序列长度 T（变长轴），不改特征维 F。换个 fps 重训的权重加载时 shape/key 全过、密度已错 —— 这是词表数量类契约（能被 shape 抓）之外、**唯一抓不到的那类契约**。
- **必须有显式重采样层**：入模前按帧 `ts` 把窗口重采样到模型契约帧率（见 [operator.py](../../../app/services/inference/temporal/operator.py) `_resample_by_ts`）；检测密度 ⟂ 模型节奏，中间隔一层，别把检测采样率焊死成模型输入率。
- **`model_input_fps` 必填 + 加载期校验**：漏配**即崩**（yaml 缺 key → 构造缺参 TypeError；非正值 → `__init__` ValueError），别缺省 `None` + warning 放行 —— warning 会被无视、skew 静默复活。`window_seconds` 与 fps 共定 T，一并按**时间密度契约**看待，不是自由旋钮。

## 🟠 会抛异常

- **bbox 硬解构** `x1,y1,x2,y2 = bbox`：`Detection.bbox` 契约变（mask/空框）即 `ValueError`。
- **除零**：`width`/`height` 为 0 → `inf`/`nan` 静默入模型。

## 🟡 算子生命周期 / 因果性

- **状态全在 `_sm`**：有无逸出成员（`history_frames` 类跨 tick 累加器）？破坏单 `_sm` 不变式。
- **别重复造窗**：`analyze` 收到的 windows 已被 `_zip_by_ts`/`_clip` 按 `window_seconds` 裁到感受野；算子自己再攒一层历史 = 冗余 + 窗口随 tick 漂移。直接喂整窗。
- **游标防重**：`last_ts` 跳过已推理帧，否则重叠滑窗重复前向。
- **因果性（硬门）**：实时链路模型必须因果 —— 单向 GRU / causal mask。双向、或需未来帧才判定（MS-TCN 类）**不能进 1Hz 实时 tick**，走离线链路。感受域 ≥ 窗口；不足加显式 warm-up guard（帧数 < 阈值不推理）。**确认 checkpoint 出自因果 pipeline**（训练仓 `sliding_window_temporal`），别把离线双向那版（`full_sequence_temporal` / BiGRU）拿来在线读 `logits[-1]` —— 最后一步后向上下文为零 = 静默误分类，且 jit blob 看不出它是否双向，得回训练仓核对产物来源。
- **推理 seam**：惰性加载（双检锁）+ `model.eval()` + `torch.no_grad()`；torch/模型 import 下沉、模块顶层不引 torch（同 [detector.py](../../../app/services/inference/detection/detector.py) 把 ultralytics 延迟到加载处）；权重缺失显式报错。

## ⚪ 契约 / 放置 / 风格

- **Operator 契约**：`analyze` 推 `_sm` 不返回、`judge` 读 `_sm` 出 `(events, alarms)`；别塞领域字段，派生量走 `Detection.extra` / `FrameDetections.metadata`。
- **realtime 标志**：`true` = 纳入 signals_10s（会触发 `AlarmMetric(stream.upper())` 映射）；纯 overlay 无告警可设 `false`。
- **放置**：可复用模型基类（如 `GRUOperator`）放 [operator.py](../../../app/services/inference/temporal/operator.py) 抽象 `Operator` 旁，与 `YOLODetector`/`Detector` 同构；具体规则放 `workflows/`。
- **风格**：类/模块 docstring 对齐同层写法；未接入的模型（如闲置 transformer）标注"离线/实验预留"，别当死代码留白；行尾空格 / EOF 换行。

## YAML 装配 — 配置随产物，不进部署 YAML（核心不变式）

**不变式（重点）**：模型固有量随产物走，部署 YAML 只留编排。具体存法待定，别让实现之争盖过这条原则。

判据一句：**能自由调的才配；必须与另一产物严格一致的不配（随产物走）。** 用"配错了会怎样"分流：

- 配错后**行为变、不崩、可调**（`subscribes`/`realtime`/`min_frames`/`model_path`）→ 真配置，留 YAML。
- 配错后 **加载即 shape/key mismatch**（`hidden`/`num_layers`，以及 `objects`/`actions` 的**数量**——它们定 `input_dim = count × 每物体特征数` 与 `num_classes`）→ 这不是配置，是"必须逐位等于产物的契约副本"。放 YAML 只把单一真源劈成"权重+YAML"两处、制造漂移（换个重训权重就得记着同步改），失去配置驱动的意义、流程反更繁琐。label 字符串是词表里唯一"软"的（仅 overlay 显示），也随产物走。
- 配错后 **不崩、不 shape-mismatch、静默喂错时间密度**（`model_input_fps`，及改变帧数的 `window_seconds`）→ 最阴的一类：skew 温床，shape-check 天生漏判（fps 改 T 不改 F）。至少**必填 + 加载期校验**（见上方 🔴 train/serve skew）；理想随产物走（下方 sidecar），别在部署 YAML 手抄一个 fps 数字。

**实现待商榷（列菜单，别钦定）**——共同点都是配置跟着权重、不进 YAML：

| 方式 | 配置/label 放哪 | 备注 |
|------|----------------|------|
| torch.save 一个 dict `{state_dict, arch, id2label}` | 同一 `.pt` | 最省事；`weights_only=True` 加载纯基元 dict 可规避 pickle RCE |
| safetensors + `config.json` 旁挂（HF 式 `id2label`） | 独立 JSON | 可读可 diff、不加载权重就能看、无代码执行 |
| safetensors `__metadata__` 内嵌 | 权重文件头（值限字符串） | 单文件、安全 |
| TorchScript `jit.save(_extra_files=…)` / `torch.export` | 图自带 arch，label 走附件 | arch 归零；jit 在被 export 取代 |

> 诚实排序（此项目一 GRU、一仓一模型）：safetensors+config ≳ torch.save dict ≫ 现状（arch 抄进 YAML）。别上 registry/onnx——单模型属过度。安全性（safetensors / `weights_only=True`）是当代实践一环，值得一并考虑。

⚠️ 产物格式是**跨仓契约**（时序模型一模型一仓库），schema 需与训练侧约定，别单方拍板。审 YAML 时数一下 `params`：模型架构/词表超参 + **帧率/窗口密度**（`model_input_fps`/`window_seconds`）占了一大半，就是该下沉的信号。
训练仓（Cleansight_models）本就成对维护 `external_checkpoints/<id>/<id>.yaml`，登记 feature version / tensor shape / 类别顺序 / normalization / 窗口 —— 交付时让后端读这份 sidecar，别在后端 yaml 手抄。注意：它的严格加载器查 param-key + tensor-shape，**恰好抓不到 fps**（fps 改 T 不改 shape），所以 fps 是最该随 sidecar、最不能手抄的一项。

## 上线门禁（缺一不合入）

模型接入前三项必填：**推理延迟**、**感受域（帧数）**、**模型参数量**。训练/实验代码另置独立仓库，不进本仓。

## 输出格式

按 🔴/🟠/🟡/⚪ 分档列出，每条：`文件:行 + 一句缺陷 + 具体失败场景（什么输入 → 什么错误输出）`。静默错必给"什么输入下结果错"，否则读者无从判断真伪。
