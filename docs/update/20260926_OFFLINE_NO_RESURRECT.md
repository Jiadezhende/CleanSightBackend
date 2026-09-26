# 离线写入不重建已回收目录：`create=False` + `DirectoryGoneError`

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

`write_temporal` 和 `write_label_probs` 新增关键字参数 `create`，默认 True，行为不变。
传 `create=False` 时，如果域目录已被回收，函数抛 `DirectoryGoneError`（声明在 `app/utils/exceptions.py`），不会重建目录。
离线 runner 的两处写入都改为 `create=False`，捕获到这个异常时判为 `superseded`。

## 变更背景

- **现状**：runner 写入前会最后核对一次输入戳，但核对完到真正写盘之间还有一段时间。如果这段时间里 TTL 清理（`cleanup_worker` 在自己的线程里 rmtree 整个 step）删掉了这个 step，原来的 `create=True` 会把目录重建出来，结果是一个只有 `inference/temporal.jsonl` 的僵尸 step：
  - 它的 mtime 是新的，要再过 15 天才会被 TTL 回收；
  - 它会出现在「有离线结果的任务」列表里，但没有录像。
- **触发来源**：admin 可以对任意旧 task 提交离线推理（[20260926_OFFLINE_ADMIN_SUBMIT](20260926_OFFLINE_ADMIN_SUBMIT.md)），快过期的 step 也会被跑到，这个缺口因此被放大。
- **承接**：输入戳校验（[20260926_OFFLINE_SUPERSEDE_CHECK](20260926_OFFLINE_SUPERSEDE_CHECK.md)）只能在核对的那一刻发现变化，核对之后的窗口它管不到。

## 方案详情

### 全景

```text
runner（戳核对通过后）
  write_label_probs(create=False) ─┐
  write_temporal(create=False)    ─┴─ DirectoryGoneError → superseded「检测结果目录已被回收」，盘上什么都不留
存储层 create=False：不 mkdir；写 tmp / os.replace 抛 FileNotFoundError 且域目录此刻确实不在
                    → DirectoryGoneError（from 原异常）；目录还在时的 FileNotFoundError 与其他 OSError 原样上抛
```

规则：**谁有权删、谁才有权建。** 离线 runner 是迟到的写者，不是这一代数据的开创者，所以不建目录。判据借用文件系统原生的原子失败：目录不在，`open` 和 `os.replace` 就一定失败。

| 部件 | 落在哪 |
|------|--------|
| 异常类 | [`app/utils/exceptions.py`](../../app/utils/exceptions.py) `DirectoryGoneError(AppError)`：带 `path` / `task_id` / `step_id`，不可重试 |
| 转换点 | [`_temporal._gone_as_error`](../../app/storage/inference/_temporal.py)：由掌握坐标的这一层转换；`_jsonl.write_atomic` 只负责「不 mkdir」 |
| 存储层依赖白名单 | `tests/test_import_hygiene.py` 的 `LAYER_PACKAGES["app/storage"]` 只放开 `app.utils.exceptions` 这一个模块（异常类型是跨层协议） |
| 调用方 | [`OfflineRunner.run`](../../app/services/inference/offline/runner.py)：`_maybe_write_label_probs` 让这个异常上抛，不当作旁路失败吞掉 |

### 保留项

- 默认值仍是 `create=True`：开创者（以后的在线写者）和现有调用方行为不变。
- **不防 ABA**：如果同一路径被新一代 run 重新建出来，目录又存在了，这一层照样放行。这种情况仍由输入戳校验兜住。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 核对之后 step 已被 TTL 删完 | 重建出僵尸 step | superseded，盘上不留任何东西 |
| 核对之后 TTL 的 rmtree **正在进行** | 重建出僵尸 step | **仍可能残留**：tmp 建在 rmtree 列目录之后 → rmtree 末尾 rmdir 失败 → 半删目录里留下新文件 |
| 这种情况在日志里的样子 | 无（静默重建） | 一条 warning：`superseded ... 检测结果目录已被回收` |

**自测结果**

| 项 | 结果 |
|----|------|
| `TestNoResurrect`（存储层 9 条：目录不在时抛异常且不建目录、目录存在时正常写入、tmp 写完后目录被回收、其他 OSError 原样上抛、默认参数仍建目录） | passed |
| `TestOfflineRunnerNoResurrect`（写事实前 / 写概率前被回收，均 superseded 且无僵尸） | passed |
| `TestReplaceSegments` 改为先落检测结果（与真实调用一致：runner 只在读到检测结果后才写） | passed |
| 全量 `pytest tests/` | 973 passed |
| CLI 真实子进程冒烟 | 行为不变（skipped） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **本批未闭合 TTL 冲突**（2026-09-26 复核补记）：`create=False` 这一侧是原子的，但 TTL 的 `rmtree` 是「列目录 → 逐个删 → rmdir」复合写，写入插在中途会留下半删目录 | 与进行中的 TTL rmtree 并发时仍会残留僵尸 step | 判据本身原子，缺口在 TTL 的 `rmtree` 是复合写；闭合办法是把 TTL 删除改成先 `rename` 到回收区再删，见 [DESIGN_STALE_WRITES](../kb/DESIGN_STALE_WRITES.md) |
| recording 的写路径仍全部 `create=True`，拆除后迟到的残段在 TTL 之后同样会重建僵尸 | 已记录在 `tasks.py` 的「已知窄缺口」，要连跑 15 天才会触发，当前任务 30 分钟超时，触发不到 | 单独一批：只有认领分支（本代首写）建目录，其余写入 `create=False` |
| 同路径重建（ABA） | 换代恰好落在戳核对与写入之间的毫秒窗口 | 接受；要严格时再加读侧版本校验 |
