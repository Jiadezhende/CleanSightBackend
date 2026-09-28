# 时间量纲统一为整数毫秒：run_id、段名、HlsSpan 不再用微秒

> **变更状态**：生效中（2026-09-28）；盘上旧微秒数据须先跑一次性迁移脚本（不入库）才能被读到
> **知识库**：待沉淀

## 概述

`run_id` 改为分配时刻的 epoch 毫秒，段身份键 `SegmentRef.ts_us` 改为 `ts_ms`（段文件名、`.idx`、stage 目录、`.m3u8` 里的段名随之改），`HlsSpan` / `runs.query_lifespan_ms` 返回毫秒，routers 里的 `// 1000` 全部删掉。对外只有 `run_id` 的数值变了（÷1000），其余时间字段数值不变。与盘上旧数据**不兼容**，代码里没有兼容分支。

## 变更背景

- **现状 / 痛点**：`run_id` 和段名用微秒，只是历史上的命名选择，没有任何精度用途（段 ~10s、告警与 API 全是毫秒）。消费方每次都要 `// 1000`：task / traceback / lab 三个 router、`PlacedSegment.wall_start_ms` 都在折算，漏一处就差 1000 倍。
- **规则（人已拍板）**：**我们自己产出、落盘、对外暴露的时间量一律用整数毫秒**。浮点秒只留在外部格式规定的地方：ffmpeg 参数、HLS `#EXTINF`、解码侧 `Frame.timestamp` 和由它派生的 `detections.jsonl` / `temporal.jsonl` / `label_probs.npz` 里的 `ts`。ffmpeg `-timeout`（RTSP 读超时）规定用微秒，保持不动。
- **承接**：建立在 run 目录 `{task}/{step}/{run_id}/` 布局（`20260927_STORAGE_RUN_DIR_PROPOSAL.md`）和 `hls.query_span` / `runs.query_lifespan_*`（`20260928_STORAGE_QUERY_ADD.md`）之上。

## 方案详情

### 全景：一个时间量从产生到出口

```text
runs.allocate           run_id = max(time_ns // 1_000_000, 已有最大 + 1)      ← 目录名 {run_id}
hls.insert_segment      ts_ms = ts_to_ms(frames[0].timestamp)                ← 段名 / idx / stage / m3u8 URI
  └ ts_to_ms(ts)        floor(Fraction(ts) * 1000)   按 float 精确值向下取整
hls.query_span          start_ms / last_start_ms / end_ms = max(ts_ms + round(EXTINF*1000))
runs.query_lifespan_ms  (run_id, successor)           直接当毫秒区间用
routers                 原样透传，不再 // 1000
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| run_id 分配、存续区间 | [`app/storage/runs.py`](../../app/storage/runs.py) | §1 |
| 段身份键与命名 | [`app/storage/hls/_layout.py`](../../app/storage/hls/_layout.py)、[`types.py`](../../app/storage/hls/types.py) | §2 |
| 跨度与媒体轴 | [`_read.py`](../../app/storage/hls/_read.py)、[`_timeline.py`](../../app/storage/hls/_timeline.py) | §3 |
| 消费方 | `app/routers/{task,traceback,lab}.py`、`app/services/recording/service.py`、`app/static/{lab,admin}/index.html` | §4 |
| 落盘格式变化（迁移脚本输入） | — | §5 |

### 1. run_id 改为墙钟毫秒

`runs.allocate` 里的 `time.time_ns() // 1000` 改成 `// 1_000_000`，单调守卫 `max(now, 已有最大 + 1)` 不变。同一毫秒内连续分配时 +1，run_id 最多领先墙钟几毫秒，对「按存续区间过滤告警」没有可见影响。`query_lifespan_us` 改名为 `query_lifespan_ms`，返回值本来就是 run_id，不需要换算。

### 2. 段身份键 `ts_ms`：按精确值向下取整

读侧 `bisect_right - 1` 依赖「段名 ≤ 段内首帧 ts」。`int(ts * 1000)` 保证不了这一点：乘法本身会舍入，`0.29 * 1000 == 290.0`，但 0.29 的 float 精确值是 0.28999…，结果比首帧晚。`ts_to_ms` 改用 `math.floor(Fraction(ts) * 1000)`，这样三种比较口径都满足「段名 ≤ 首帧」：精确值、`ts_ms / 1000 <= ts`（`SegmentRef.ts_s`）、`ts_ms <= ts * 1000`（`list_segments_in_range` 的 bisect 键）。对真实墙钟 ts（~1.7e9，带亚毫秒噪声），两种写法只在极少数样本上差 1ms。

`_layout` 仍然只依赖 stdlib（`math`、`fractions`）。

### 3. `HlsSpan` 改为毫秒

字段改名为 `start_ms / last_start_ms / end_ms`，其中 `end_ms = max(ts_ms + round(EXTINF × 1000))`。旧算法是 `(ts_us + round(EXTINF × 1e6)) // 1000`，因为 EXTINF 落盘就是三位小数，两者在实际数据上相等。只有段名取整恰好落在边界的病态样本上，段尾会差 ≤1ms，可以接受。`PlacedSegment.wall_start_ms` 直接取 `ts_ms`。

### 4. 消费方

- routers：`task._summarise_steps`、`traceback.get_timeline`（包括存续区间）、`lab._storage_task_to_item` 删掉 `// 1000`。
- `recording` 队列 label：`seg:…@{ts_ms}`。
- 前端：lab 的 `formatTaskTime` 删掉秒 / 微秒猜测，直接按毫秒处理（`/lab-f3m8/tasks` 两种模式都是毫秒，DB 模式已在 dev 库核实 `clean_task.updated_time` 为 epoch 毫秒）。admin 的 `formatTaskTime` 删掉微秒分支，秒分支保留，因为离线作业的 `submitted_at` 是秒。
- `docs/api`：README、traceback、task、ai、admin、lab、health、_TEMPLATE 里的 run_id 示例和「微秒」说法全部改成毫秒；lab `/tasks` 的 `updated_time / start_time / end_time` 在两种模式下都标为 epoch 毫秒。

### 5. 落盘格式变化（迁移脚本的输入）

路径相对存储根 `{root}`。下表中「→」前后分别是旧格式和新格式。

| # | 路径模式 | 位置 / 字段 | 旧 → 新 |
|---|----------|-------------|---------|
| 1 | `{task}/{step}/{run_id}/` | run 目录名 | epoch 微秒 → epoch 毫秒（`old // 1000`；同 step 内若撞名，按升序取 `前一个 + 1`，保持严格递增） |
| 2 | `{task}/{step}/{run_id}/hls/{track}_segment_{ts}.mp4` | 文件名里的 ts | 微秒 → 毫秒（`old // 1000`） |
| 3 | `{task}/{step}/{run_id}/hls/raw_segment_{ts}.idx` | 文件名里的 ts（与同名 mp4 同键） | 微秒 → 毫秒（`old // 1000`）；**内容不变**（float64 帧 ts 秒） |
| 4 | `{task}/{step}/{run_id}/hls/{track}_playlist.m3u8` | 每个 `#EXTINF` 下一行的段 URI `{track}_segment_{ts}.mp4` | 微秒 → 毫秒（与 #2 同一映射）；`#EXTINF`、`#EXT-X-MAP:URI="{track}_init.mp4"`、头部不变 |
| 5 | `{task}/{step}/{run_id}/hls/.stage_{track}_{ts}/` | 写入暂存残留（崩溃才会留下） | 直接删除，不迁移 |
| 6 | `{task}/{step}/{run_id}/hls/.clip_*.m3u8`、`.export_*.m3u8` | lab 临时 VOD 清单残留（内容里是旧段名） | 直接删除 |
| 7 | `.trash/` | `utils.fs.remove` 回收区，里面可能有旧命名目录 | 直接清空 |

**确认不含微秒、不需要迁移**的产物：

| 路径 | 内容 |
|------|------|
| `hls/{track}_init.mp4` | 二进制 |
| `hls/metadata.json` | `start_time`（int 秒）、`*_segments.first_timestamp / last_timestamp / total_duration`（float 秒）、ISO 字符串；不含 run_id 或段名 |
| `inference/detections.jsonl` | `ts`（float 秒） |
| `inference/temporal.jsonl` | `ts / start / end`（float 秒）、`meta`（目前只有 `model_version`） |
| `inference/label_probs.npz` | `ts`（float64 秒） |
| `.lab_exports/` | `clip_{start_ms}_{end_ms}.mp4`（原本就是毫秒）、`step_*_{nonce}.mp4`，30 分钟孤儿清扫 |
| `lab_runtime_config.json` | 不含时间量 |

**不落盘但会受影响**：媒体 token（payload 里有 `run_id` 和段文件名，TTL 300s）和内存里的离线作业记录。迁移时停后端即可，重启后二者自然失效。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| `run_id` 数值 | ≈1.8e15（微秒） | ≈1.8e12（毫秒） |
| 段名 | `raw_segment_1700000000123456.mp4` | `raw_segment_1700000000123.mp4` |
| routers 里的 `// 1000` | 9 处 | 0 |
| 对外时间字段（`start_ms` / `end_ms` / `last_segment_ms` / lab `updated_time` 等） | 毫秒 | 数值不变 |

**自测结果**

| 项 | 结果 |
|----|------|
| 新增用例 | `ts_to_ms` 向下取整（1.9999999、1.0005、2.0、1_700_000_000.123）与「不超过精确值」（0.29、0.57 等）共 9 个参数化用例 |
| `tests/test_import_hygiene.py` | 绿 |
| 全量 `pytest tests/` | 977 passed, 8 skipped（基线 969 passed, 8 skipped） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 盘上旧数据是微秒命名 | 不迁移的话，旧 run 的 `run_id` 当毫秒用会被当成远未来的时刻：它排在「最新 run」，新分配的 run_id 被单调守卫顶成 `旧值 + 1`（量级错乱），段查询找不到段 | 部署前停后端，按 §5 跑一次性迁移脚本（脚本不入库） |
| `docs/kb/` 里仍有 `ts_us` / 微秒的说法 | KB 与代码不一致 | 下一次 `/kb-merge` 时沉淀 |
| `metadata.json`、`/admin` 与 `/health` 的时间字段仍是秒 | 与「一律整数毫秒」的规则不一致（不是微秒，不在本次范围） | 另行决定是否统一 |
