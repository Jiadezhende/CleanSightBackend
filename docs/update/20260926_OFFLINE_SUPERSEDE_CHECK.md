# 离线 Runner 换代校验：输入戳变了就 superseded、不写

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

`app.storage.inference` 新增 `detections_stamp`（`detections.jsonl` 的 `(inode, size, mtime_ns)`）。`OfflineRunner.run` 在读前记戳，读完和写前各核对一次，不一致就返回新状态 `superseded`，temporal / label_probs 一律不写。

## 变更背景

- **现状**：runner 文档要求调用方保证输入已封口，但 runner 自己不做任何验证。一旦离线结果写在同 step 新一代 run 开写之后，就会把上一代的分段写进新一代的目录。原因是 recording 在新一代首次写入时会整域删除再重建（`_write_detections` 的第 ② 步）。
- **触发来源**：离线作业服务要支持从前端提交任务（下一批），需要在执行离线任务时自己判断是否已换代，不能依赖提交方。

## 方案详情

### 全景

```text
stamp = detections_stamp()          ← 读前
frames = read_detections()
stamp 变？ → superseded             ← 读的过程中被追加 = 未封口
preprocess → segment                （耗时段）
stamp 变？ → superseded，不写        ← 运行期间被追加 / 换代 / 删除
write label_probs → write temporal  （都是 tmp + os.replace 原子写）
```

| 部件 | 落在哪 |
|------|--------|
| 版本戳 | [`_detection.detections_stamp`](../../app/storage/inference/_detection.py)，只做相等比较，文件不存在返回 None |
| 三处核对 + `superseded` | [`OfflineRunner.run`](../../app/services/inference/offline/runner.py) |

### 方案选型

| 方案 | 结论 |
|------|------|
| 文件戳乐观锁，落在 runner 里（采用） | 离线层自己的机制，CLI、作业服务、以后的自动触发都能受益；语义与 recording 的代次校验一致：失败就丢弃，不重试 |
| 在 `run_control.start_run` 里取消离线 job | 只能保护经过作业服务的那条路径，而且会增加一处跨服务耦合 |
| 跨进程文件锁 | 存储层约定不持锁 |

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 离线运行期间同 step 换代 | 旧分段写进新一代目录 | superseded，不写 |
| 输入未封口（残批在运行中才落盘） | 按不完整的输入产出结果 | superseded，需重跑 |

**自测结果**

| 项 | 结果 |
|----|------|
| `TestDetectionsStamp`（4）+ `TestOfflineRunnerSupersede`（4） | passed |
| 全量 `pytest tests/` | 932 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 最后一次核对到 `os.replace` 之间还有毫秒级窗口 | 换代如果恰好落在这个窗口里，新一代目录里会多出一份旧分段 | 接受；重跑一次即可覆盖 |
| CLI 退出码 / 文档还没提到 superseded | CLI 对 superseded 已返回 0，但 docstring 没更新 | 下一批（CLI `--json`）一起改 |
