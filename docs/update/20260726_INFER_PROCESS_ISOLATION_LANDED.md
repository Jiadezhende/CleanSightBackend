# 推理进程隔离落地：req_id 异步管线消除 GIL 争用（clean 单路 ~10→稳定 15fps）

> **变更状态**：已落地并验证。单路 clean stage 从 ~10fps 天花板升到**稳定 ~15fps**（名义 `inference_fps` 目标）。
> **根因见** → [20260726_INFER_LAUNCH_BOUND_DIAGNOSIS.md](20260726_INFER_LAUNCH_BOUND_DIAGNOSIS.md)（GIL 争用 / 线程调度，非 compute-bound）。
> **知识库**：待沉淀 → `kb/SERVICE_INFERENCE.md`（L1 检测子包描述需改：GPU 前向已在独立子进程）。

## 做了什么

把 YOLO GPU 前向从主进程拆到**独立 spawn 子进程**，让推理循环独占一把 GIL——kernel 发射线程不再被主进程 CPU 大户（viz/temporal/HLS/dispatcher）抢 GIL 饿到（正对诊断的「时钟满 / SM 12% / 60W」）。采用**内部 req_id + 异步收结果**管线，`cq` 句柄不过进程边界。

**核心支点**：切口选在 `MultiModelWorkerPool._infer_models(frames, timestamps)`——进出纯数据、`cq` 不在场。`cq` 只在其上方 dispatcher 捕获、下方 `_write_back_results` 使用，留主进程按 req_id 关联即可，**句柄零序列化零重构**。

## 数据流

```
主进程                                          推理子进程（spawn，独占 GIL）
dispatcher.pop（捕获 cq）
  └ _inference_loop(stage): proxy.submit(batch) ──req_q──►  _infer_models(frames, ts)
       · req_id + pending[req_id]=轻量记录(cq,ts,w,h)  ◄─resp_q── (req_id, merged, stats)
       · 弃 frame 引用；inflight++                        （单进程 FIFO，同 stage 保序）
  └ _collect_loop（proxy 内单线程）:
       records = pending.pop(req_id)  ← 防泄漏核心
       → 组 FrameInference(cq=record.cq,…) → _write_back_results（原样复用）
       → 主进程发 infer_latency_ms / infer_failure_total（子进程 registry 无效，埋点上移）
```

**异步**：submit 不阻塞等结果，子进程前向与主进程下一批 dispatch 重叠（深度受 `max_inflight` 限）。主进程推理循环由「持 GIL 跑 CUDA」变为「queue.get 释放 GIL」，GIL 压力一并降。

## 代码

| 文件 | 改动 |
|------|------|
| `detection/infer_worker.py` | **新增** 子进程 entrypoint：先钉 `CUDA_VISIBLE_DEVICES` 再 import torch（镜像 `offline/cli.py` 隔离序，方向相反）；同一 `StageFactory` 从 YAML 自建 pool + warmup + FIFO 循环。边界只过纯数据。 |
| `detection/remote_infer.py` | **新增** `RemoteInferProxy`：submit/pending/collector/supervisor。 |
| `detection/service.py` | `_inference_loop` 改 `proxy.submit`（非阻塞、不再持 GPU）；start/stop 接 proxy；主进程不再建 `MultiModelWorkerPool`（GPU 全在子进程）。 |
| `detection/pool.py` | `_infer_models` 返回 `(merged, stats)`、剥掉跨进程无效的 Prometheus；`infer_batch` 保留为进程内/单测路径。 |
| `detection/detector.py` | 删 `[DIAG-TEMP]` 埋点（诊断已定案）。 |

## 关键不变式

- **主进程 CUDA-free**：viz 只调 `prepare_visualization_data`（CPU），从不 `infer_batch`；主进程保留的 detector 实例永不加载模型。GPU/CUDA context 只在子进程。
- **防泄漏三重**：pending 有界（`max_inflight=8`，满则 submit 拒收计 `frame_drop_total{reason=infer_inflight_full}`）；collector 每条响应 `pending.pop`；子进程死/CUDA wedge → supervisor 清空 pending（计 `infer_child_restart`）、`inflight` 归零、退避重 spawn。
- **不丢数据**：`ModelWorkerService.stop()`（含 proxy 排空在途写回）早于 `InferenceManager.stop` 的 `feature_store.flush()`——排空-先于-flush 顺序天然满足。
- **迟到/跨 run**：collector 组 `FrameInference` 后走原 `_write_back_results`，其 `cq.is_active()` 门 + `feature_store owner=cq` fence 原样生效。
- **保序**：单子进程单 `req_q` FIFO，同 stage 结果按提交序返回。
- **必须 spawn**：CUDA + fork 不安全，且要主进程 CUDA-free。子进程 spawn 后从 YAML 自建、`.pt` 惰性加载。

## 新增 `frame_drop_total` reason

`infer_inflight_full`（在途满拒收）、`infer_child_down`（stop 时未及排空）、`infer_child_restart`（子进程重启清孤儿）。

## 验证

- **生产 4090**：单路 clean 稳定 ~15fps（越过 ~10fps 天花板）。
- **单测**：`tests/test_remote_infer_proxy.py` 8 例（写回重组正确、背压拒收、部分响应泄漏边界 `pending==N-M`、子进程死清理+计丢帧、孤儿响应忽略、失败埋点）；全套 354 passed。
- **真 spawn CPU smoke**（MOCK stage 免 GPU）：子进程 boot→warmup→收真 Queue 请求→`_infer_models`→主进程重组 `FrameInference`（cq 贴回、ts/wh 对齐）→写回→pending 清零→干净 stop。

## 未做（后续增益，非阻塞）

- **TensorRT engine**：诊断列为①之后互补项（稳态 8ms→~3ms），本次未做。
- **shared_memory 零拷贝环**：1–4 路 pickle over Queue 够用；`submit`/子进程收帧留干净切口，profile 冒头再换。
- `max_inflight`/`cuda_device` 目前是 `service.py` 常量默认（YAGNI 未上 settings）。
- **`raw_fps` 配置修正**（写 30 实际 25）：独立小任务。
