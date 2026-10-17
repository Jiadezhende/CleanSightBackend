# 离线作业可用在线推理那张 GPU，策略可经 Runner 取 run 的原始画面

> **变更状态**：生效中（2026-10-17）
> **知识库**：待沉淀

## 概述

离线子进程不再禁 GPU，改为钉成 `settings.cuda_device`（与在线推理子进程同一张卡）；离线策略新增可选的
`OfflineContext` 注入口，声明 `uses_context = True` 的策略经它按 raw 轨取本 run 的帧。现有策略行为不变。
这是 CLEAN 新基准 C7（手部裁剪 → DINOv2 → 手部状态分类器）接入的前置框架改动。

## 变更背景

- **现状**：离线子进程 `CUDA_VISIBLE_DEVICES=""`，只能跑 CPU；策略约定只吃 `FrameDetection`，拿不到像素。
- **触发来源**：C7 要按检测手框裁原始画面过 DINOv2 ViT-S/14。实测（RTX 5060 Laptop）CPU 2 线程 68 ms/次，
  10 分钟 step 约 1.7 万次裁剪 ≈ 20 min，逼近 1800 s 作业超时；GPU fp16 0.82 ms/次。人定：离线开 GPU、限速，
  且和 YOLO 指定同一张卡、不碰机器上其他卡。

## 方案详情

### 全景

```text
OfflineJobService ── 子进程（env 不再改 CUDA_VISIBLE_DEVICES）
  cli run: _isolate() = CUDA_VISIBLE_DEVICES ← settings.cuda_device + torch.set_num_threads   ← 先于任何 torch import
  OfflineRunner.run
    segmenter.uses_context ? preprocess(frames, OfflineContext(video=partial(hls.iter_frames, run)))
                           : preprocess(frames)                                   ← 旧策略不变
```

| 部件 | 落在哪 |
|------|--------|
| `OfflineContext` / `VideoSource` / `uses_context` | [`segmenter.py`](../../app/services/inference/offline/segmenter.py) |
| 注入（绑定本次锁定的 run） | [`OfflineRunner._context`](../../app/services/inference/offline/runner.py) |
| 钉卡 | [`cli._pin_device` / `cli._isolate`](../../app/services/inference/offline/cli.py)（原 `_isolate_cpu`） |
| 子进程 env | [`service._child_env`](../../app/services/inference/offline/service.py) 删 `CUDA_VISIBLE_DEVICES=""` |

### 关键约定

- **钉卡只在 cli 一处**：作业服务起的子进程和手动跑 CLI 走同一条路径；`settings.cuda_device=""` 时离线也看不到 GPU。
  用不用 GPU、限多少速由策略参数决定，框架不替策略选设备。
- **`context.video` 只走 raw 轨**：帧 ts 位级等于 detections 的 ts，策略直接 `==` 匹配；processed 轨是渲染结果，不提供。
- **策略仍不碰存储**：只拿到绑定好 run 的可调用对象，不知道路径、不 import `app.storage`。

## 变更效果

| 项 | 结果 |
|----|------|
| 新增用例 | `uses_context` 策略拿到的 video 绑定锁定 run、参数原样透传；`_pin_device` 取 `settings.cuda_device` |
| 全量 `pytest tests/` | 1036 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 离线 GPU 与在线 YOLO 抢卡：实测 DINOv2 不限速时 YOLO 吞吐 232 → 76–82 img/s；限 150 次/s 时 −13% | 在线检测掉帧 | 由使用 GPU 的策略自带限速参数（C7 接入时落地），生产机复测后定默认值 |
| 子进程 CUDA 上下文显存（Windows 实测 115 MiB，Linux 通常更大） | 与在线共卡的显存预算 | 生产机复测 |
