# dispatcher 的 cap 分配与背压预案（未落地，先立判据与方案）

> **变更状态**：提案（2026-07-27）——**当前系统未做任何改动**。现状 dispatcher 在当前场景（≤2 active stage、未观测到真过载）下行为正确、够用；本篇只记录「过载区」的已知缺口与对策，等观测到触发条件再落地。
> **知识库**：无需沉淀（提案，未落地）。落地时另起一篇「生效中」记录再沉淀。
>
> 承接：建立在 [20260726_DETECTION_REPACKAGE_SUBMIT_MERGE.md](20260726_DETECTION_REPACKAGE_SUBMIT_MERGE.md) 的「单提交者」模型之上——dispatcher 是唯一提交者，每轮先读 `capacity()` 再按额度 submit。本篇讨论的正是「读到的 cap 怎么在多 stage 间分、分不到时怎么背压」。

## 概述

- **背景**：[`StageAwareDispatcher._drain_and_submit`](../../app/services/inference/detection/dispatcher.py) 每轮据 `cap = capacity()` 从各 active stage 的 deque 组批直提子进程。当前分配是**贪婪深度优先**：轮换起始 stage，但内层 `while cap>0` 会把当前 stage 排空（或 cap 耗尽）才让位下一个。
- **现状结论**：稳态（cap 充足、deque 常空）下，内层 `while` 第二次 `_pull_batch` 即 break 让位，**贪婪 ≡ 轮转排空**，无问题。缺陷只在**过载 + ≥2 stage** 时暴露，当前未触发。
- **本篇目的**：把已识别的三件事（分配公平、淘汰最旧的语义冲突、背压降帧的正确位置）连同「何时才该落地」的判据写下来，避免下次 /clear 后重新推导，也避免在没量到过载前提前上复杂度。

## 现状机制盘点（改动前基线）

| 量 | 定义 | 位置 |
|----|------|------|
| `max_inflight` | 在途批数上限，=8（`DEFAULT_MAX_INFLIGHT`） | [service.py](../../app/services/inference/detection/service.py) |
| `inflight` | 状态计数：已 submit 未回收的**批数**；dispatcher `+1`、collector `-1`，全程 `self._lock` | [infer_proxy.py](../../app/services/inference/detection/infer_proxy.py) |
| `cap` | 派生量 `= max(0, max_inflight - inflight)`，现算；**唯一消费者是 dispatcher，单提交者下可内化掉（见「职责边界修正」）** | [`capacity()`](../../app/services/inference/detection/infer_proxy.py) |
| `batch_size` | 每批帧数上限，per stage（`stage_batch_sizes`） | [service.py](../../app/services/inference/detection/service.py) |
| stage deque | `maxlen=256`，跨轮累积；满则 append 从**左端**淘汰最旧，`_stage_drops` 计数 | [dispatcher.py](../../app/services/inference/detection/dispatcher.py) |

关键不变式：cap 单位是**批**不是帧；子进程单进程 **FIFO 串行**前向，在途越深尾延迟越大。**cap 分配不改变总吞吐**（吞吐由子进程定死），只决定过载时的公平/延迟分布。

## 问题分析

### 缺口 B：贪婪深度优先 → 过载锯齿饥饿

`cap=8`、两 stage 都积压 `>batch_size` 时，起始 stage 的内层 `while` 会一口气把 8 个 cap 吃光，另一 stage 本轮 0 批；下轮起始轮换，变成对方吃光。表现为 **8/0、0/8 交替的锯齿**，而非期望的 4/4 平滑均分。对下游时序算子（对供帧连续性敏感）尤其有害。

> 只在 deque 积压 `>batch_size` 时触发；积压 `≤batch_size` 时一次 `_pull_batch` 就拉空、每 stage 每轮仅 1 批，贪婪自动退化为轮转，无锯齿。

### 堆积的两个成因，对策不同（分诊后用药）

| 成因 | 本质 | 信号（看 `[INFER_PRESSURE]`） | 对策 | 是否丢帧 |
|------|------|------------------------------|------|---------|
| **A：某 stage 流多** | 输入不均衡，总吞吐**够** | 单 stage deque 深、其他浅，inflight **未长期贴满** | 分配层：调该 stage `batch_size` / 加权轮转 | 否 |
| **B：下游推理跟不上** | 总流入 > 子进程吞吐 | **所有** stage deque 一起涨，inflight 长期贴 max | 总量层：降帧 / 换小模型 / 换 GPU | 必然 |

分诊判据 = **inflight 有没有余**。给成因 B 的 stage 加 `batch_size` 无效（发再多只是排进 FIFO 等）——用错药的典型。

### 淘汰最旧 vs FIFO 消费：同端，语义打架（非正确性 bug）

`deque(maxlen)` 满时 append 从**左端**挤出最旧，消费 `popleft` 也取**左端**——同一端、同一批候选帧。过载时，这轮 append 挤掉的最旧帧正是这轮想消费的那帧，**从没被推理就没了**；消费者永远追一个「从头被砍」的队列，**延迟被钉死在 `maxlen × 帧间隔`（256 帧）**。

- **无正确性问题**：dispatcher 单线程顺序执行 `_fetch`（append）→ `_drain`（popleft），两处又同在 `_lock` 下，不是并发赛跑。
- **真问题不在「丢最旧还是最新」，而在「丢得太晚」**：等 deque 满了才被动淘汰。解法见下。

### 背压降帧必须在 dispatcher，不在子进程

子进程只「收到帧就推」，不懂帧率、不懂时间——**降帧只能由 dispatcher 决定送哪些帧**。正确形态是把背压从「deque 满被动淘汰」上移到「入队时主动按 ts 相位抽稀」：过载时 dispatcher 在 `pop_ca_ready` 后、`append` 前降低送推理的目标 fps（丢帧但每帧 ts 保真）。这**顺带解掉上一节**：入口就降帧 → deque 不满 → 无「追砍头队列」、延迟不再钉在 maxlen。

两个必须守的边界：
1. **与 temporal 的 [`_resample_by_ts`](../../app/services/inference/temporal/operator.py) 不冲突**：那个是恒定的 train/serve fps 对齐（固定 `model_input_fps`），这个是过载动态降帧；两者都以 **ts 为货币**、都「遇缺口重锚不追补」，叠加安全。前提是降帧保 ts 真实（时间为唯一货币）。
2. **代价：viz 帧率同步降**（viz 也吃 `latest_inference`）。成因 B 过载下不可避免的取舍——保推理正确性，牺牲画面流畅。

### 职责边界修正：背压归 proxy，dispatcher 去掉 capacity

`capacity()` 是「单提交者化」的中间产物、可内化。多提交线程时代靠「先读 cap 再发」防互撞假丢帧；合并成单提交者后，它只剩「把 proxy 私有的 `inflight` 状态泄漏给 dispatcher」这一个作用。而 [`submit`](../../app/services/inference/detection/infer_proxy.py) 早已返回布尔（内部 `if inflight >= max_inflight: return False`）——**proxy 本就能独管背压**。grep 确认 `capacity()` 唯一消费者是 dispatcher（无观测/测试依赖），可安全删除。

| | 现状 | 应然 |
|--|------|------|
| **proxy** | `capacity()` + `submit()` | **独管**在途上限：`inflight`/`max_inflight` 私有，`submit` 布尔返回即背压信号 |
| **dispatcher** | 读 `capacity()`、贪婪 `while cap>0` | 只做取帧/轮转/组批，**零推理侧感知**；每 stage 每圈 peek 一批，被拒即停 |

**正向副产品**：现状 `submit` 返 False 时 batch 已 `popleft`，故计 `infer_inflight_full` 丢帧；peek-commit 下 batch 没取出 → **正常背压不丢帧**，该计数器可删（背压从「丢帧事件」降为「本轮少发」）。

## 预案（分层对策）

```
堆积
├─ A 某 stage 流多（inflight 有余）→ 分配层
│     ① 轮转排空 + 背压内化：外层 while 转圈、每 stage 每圈 peek 一批，submit 被拒即停；
│        删掉 dispatcher 的 capacity 依赖（背压归 proxy，见「职责边界修正」）
│     ② 帧级加权用 batch_size（cap 单位是批，batch_size 即隐式帧权重），不上 WRR
└─ B 吞吐跟不上（inflight 贴满）→ 总量层
      ③ dispatcher 入口按 ts 主动抽稀降帧（早丢/均匀/保 ts），替代 deque 满被动淘汰
      ④ 真正治理靠降帧目标 fps / 换小模型 / 换 GPU；分配救不了吞吐
```

轮转排空 + 背压内化参考实现（替换 `_drain_and_submit`，dispatcher 不再持 `capacity`）：

```python
# 每 stage 每圈 peek 一批；proxy 布尔背压（submit 内部 inflight<max 才接）
offset = self._round_counter % n
while True:
    progressed = False
    for k in range(n):
        stage = self._active_stages[(offset + k) % n]
        batch = self._peek_batch(stage, self._stage_batch_sizes.get(stage, self.max_batch_per_stage))
        if not batch:
            continue
        if self._submit_batch(batch):          # 接了才真正移除
            self._commit_pop(stage, len(batch))
            progressed = True
        else:
            return                             # proxy 满：本轮停发，帧原封留 deque，不丢帧
    if not progressed:
        return                                 # 全空
```

> `_peek_batch` 切片看不移除、`_commit_pop` 成功后 `popleft`；单线程顺序 peek→submit→pop 无 TOCTOU。构造不再注入 `capacity`；`submit` 布尔即背压。

**明确不做（当前场景下的过度设计）**：按 deque 深度比例分 cap（成因 B 下多给也是排队，收益存疑）、加权轮转 WRR（无 stage 优先级差异）、帧级 cap（GPU 按批处理）。

## 落地触发条件（在此之前不动）

用 `[INFER_PRESSURE]` 观测，满足对应条件才落对应层：

- **落 ①②（轮转排空）**：观测到 `≥2 active stage` **且** 某 stage deque 持续逼近 maxlen、`drop delta>0`，同时 inflight **未**长期贴满（成因 A）。
- **落 ③（入口主动降帧）**：观测到 inflight **长期贴 max**、所有 stage deque 一起涨、`ca_processed` 也在丢（成因 B）。
- **可顺手评估（低成本，仍需先量）**：deque `maxlen=256` 是延迟上界的直接来源；若观测到稳态队列深度远小于 256，可调小以压低过载时的延迟上界——但同样先量再改。

## 现状设计合理性评价

**结论：当前场景（≤2 stage、未过载）下设计合理，无需改动。** 已识别的缺陷全部集中在「过载区」，属「正确但未来才需要补」，不是现在的 bug。

| 维度 | 评价 |
|------|------|
| 单提交者消竞态 | ✅ 优秀。`inflight` 只被 dispatcher `+1`、collector `-1`，读 cap 后按额度提交无 TOCTOU 假丢帧 |
| 背压职责边界 | ⚠️ `capacity()` 把 proxy 私有的 `inflight` 泄漏给 dispatcher；单提交者下应内化为 `submit` 布尔背压（proxy 独管、dispatcher 零感知）。落 ① 时一并做 |
| 稳态分配 | ✅ 合理。cap 充足时贪婪 ≡ 轮转，简单且够用 |
| 可观测性 | ✅ 合理。`[INFER_PRESSURE]` 按 stage 暴露深度/丢帧 delta + inflight，足以分诊 A/B；背压信号不静默 |
| 过载分配公平 | ⚠️ 有缺口 B（锯齿），但当前 n 小未触发 |
| 淘汰最旧 | ⚠️ 保新鲜方向对，但「满了才被动丢」使延迟钉在 maxlen；过载才咬人 |
| 背压降帧 | ⚠️ 缺「入口主动降帧」，仅有被动淘汰兜底；过载才需要 |

## 验证（落地时补，本次无代码改动）

| 项 | 结果 |
|----|------|
| 本次代码改动 | 无（纯预案） |
| 落地①②后 | 新增单测：cap=N、多 stage 均积压时各 stage 获批数应均衡（±1 批），非全给起始 stage |
| 落地③后 | 新增单测：过载下入口按 ts 抽稀后送推理帧序 ts 单调、密度≈目标 fps；deque 不再触达 maxlen |
