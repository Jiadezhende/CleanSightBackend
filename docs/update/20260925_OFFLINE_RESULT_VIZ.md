# admin 离线推理 tab：视频 + 分割段 / 类别概率两条泳道（只读）

> **变更状态**：生效中（2026-09-25）
> **知识库**：待沉淀

## 概述

admin 页新增「离线推理」tab：选 task/step 后在视频下方画两条共享媒体时间轴的泳道——`temporal.jsonl` 的分割段与
`label_probs.npz` 的逐帧类别概率；点类别只高亮该类、其余淡化。新增 `POST /ai/temporal`、`POST /lab-f3m8/label-probs`，
`GET /lab-f3m8/tasks` 追加 `offline_steps`。只读：离线推理仍用 CLI 手动跑，本批不做调度。

## 变更背景

- **现状**：离线结果只能 `cli query` 打印 JSON；逐帧概率（[20260925_LABEL_PROBS_BYPASS](20260925_LABEL_PROBS_BYPASS.md) 落地）没有读口的消费方。
  CLEAN 分段是逐帧 argmax 经「idle 断开 → 同类合并 → 丢弃短于 `min_duration_s`」得到的，只看分段看不出哪里是模型没把握、哪里是后处理切掉的。
- **触发来源**：离线推理结果可视化需求（admin 页看全量逐帧预测与后处理后的分割）。
- **承接**：建立在 [20260925_UI_MOUNT_UNIFY](20260925_UI_MOUNT_UNIFY.md)（admin 经 `/ui-f3m8/vendor/` 取到 `hls.js`）与 LabelProbs 旁路之上。

## 方案详情

### 全景

```text
CLI 手动跑离线 → {step}/inference/temporal.jsonl + label_probs.npz
                                   │
admin「离线推理」tab                 ▼
 ① GET  /lab-f3m8/tasks          每个 task 的 offline_steps（有离线结果的 step）→ task / step 下拉
 ② POST /ai/temporal             {task_id, step_id, type: "segment", track} → 分割段（媒体刻度）
 ③ POST /lab-f3m8/label-probs    {task_id, step_id, track}                  → 逐帧概率 [C][T]（媒体刻度）
    GET  /traceback/task/{task_id}/playlist.m3u8?step_id=&track=              既有，视频
 ④ 前端：<video>(hls.js) + 泳道 A 分割段（②）+ 泳道 B 概率（③）
```

| 部件 | 落在哪 |
|------|--------|
| 清单 `offline_steps` | `app/routers/lab.py` `_list_offline_steps` |
| ② 分割段 | `app/routers/ai.py` `_temporal_view` |
| ③ 逐帧概率 | `app/routers/lab.py` `_label_probs_view` |
| ④ 页面 | `app/static/admin/index.html`「离线推理」tab |
| 请求约定 | `docs/api/README.md`「推理结果读取端点：身份键与类型走 JSON body」 |

### 方案选型

| 项 | 方案 | 结论 |
|----|------|------|
| 端点归属 | ② 归 `/ai`，③ 归 `/lab-f3m8` | 分割段是推理的正式结果；逐帧概率是模型调试旁路 |
| 身份键 | `POST` + JSON body 带 `task_id` / `step_id` / `type` / `track` | 路径只表达资源类别；形状用 `type` 区分，取值同落盘判别字段 |
| 概率呈现 | C 条折线叠在同一时间轴，选中一类其余淡化 | 同屏比较各类相对高低与交叉点；热力图读不出差值 |
| 时间换算 | 后端逐帧 `MediaTimeline.media_ms_at` | 前端只认媒体轴；10 min step 约 9000 帧 × 60 段，几十毫秒，不新增批量 API |
| 选 step | 复用 lab 清单，追加 `offline_steps` | lab 清单已按回放落盘情况查 step，再补一列即可 |

### 1. 端点形状

- `track`：`raw`（默认，清单的 `raw_steps` 保证 raw 轨有段）/ `processed`。该轨无段 → `NotFoundError`（与 traceback playlist 同一 404 体）。
- ② `items`：`TemporalSegment` 按 `start` 排序，每条 `{label, start_media_ms, end_media_ms, conf, producer}`；没有则 `[]`。
  `type` 只开 `"segment"`：`TemporalEvent` 今天零生产者。
- ③ `probs` 为 `probs.T.round(3)`（`[C][T]`，一类一条曲线）；没有概率产物 → `labels / media_ms / probs` 均 `[]`，200。
- 契约全文见 [docs/api/ai.md](../api/ai.md)、[docs/api/lab.md](../api/lab.md)。

### 2. 页面

布局与交互沿用 lab 送标工作台（2026-09-25 追加：初版的两级下拉改为下述形态，与 lab 统一；tab 位于「实时监控」之后）：

- ① 选择步骤：`task_id` / `step_id` 输入框 + 「加载」「重置」；下方 lab 同款任务表（task_id / 状态 / 离线 steps / source_ip / 更新时间 / 操作），
  工具栏有搜索框（透传 `q`）、刷新、条数，只列 `offline_steps` 非空的 task。点行 / step 标签 / 「选择」**只填表单**
  （同 task 保留已填 step，否则取第一个有结果的 step；已加载时换目标先重置），点「加载」才出结果。输入框可直接填清单外的 step。
  表下一行提示 CLI 命令。切到本 tab 拉一次清单，无轮询。
- 加载反馈：该轨无录像段（404）→ 提示确认录制或换轨；有录像但无离线结果 → 照常出视频并提示先跑 CLI。
- ② 分割结果：标题栏带轨道单选与「时长 · 段数 · producer」；左列视频 + 下方泳道框，右列 320px 侧栏（当前播放 / 当前分段 / 类别点选），同 lab 的 `player-layout`。
- 视频：hls.js 播放 `/traceback` playlist，写法同 lab 页。
- 配色：③ 的 `labels` 下标取固定色板，`idle` 灰；② 里不在 `labels` 的类名（如 MOCK 的 `mock_action`）续取。侧栏类别列表即图例。
- 泳道 A：Chart.js 横向 floating bar；泳道 B：Chart.js 折线（`parsing: false` + LTTB decimation）。两图 x 轴均为 `[0, media_duration_ms]`，y 轴区钉同宽以逐像素对齐。
- 交互：点类别 → 选中类加粗 + 填充，其余淡到 15%，两条泳道同步；点泳道任意处 → 视频跳到该时刻；播放游标竖线跟随 `currentTime`（`requestAnimationFrame` 节流，只重绘不重算）。
- 空态：无离线结果的 task（提示 CLI 命令）/ 0 段 / 模型不产概率。

### 3. 保留项（不改动）

- 离线推理的运行方式（CLI 手动）、`OfflineRunner`、落盘格式。
- 存量 GET 端点的身份键写法（`/traceback/...?step_id=`、`/lab-f3m8/download?...`），README 列为存量例外。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 看分割结果 | `cli query` 打印 JSON | 视频下方彩色分割段，点击跳播 |
| 看模型置信 | 无消费方 | 逐帧各类概率曲线，单类高亮 |
| 核对对错 | 手工按 ts 翻录像 | 游标与视频同步，泳道与画面同屏 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_ai_temporal_router.py`（新） | 7 passed：媒体刻度换算（含停顿吸附）与排序、TemporalEvent 不出、无结果 `[]`、该轨无段 404、缺字段 / 非法 `type` / 非法 `track` 422 |
| `tests/test_lab_label_probs.py`（新） | 6 passed：`[C][T]` 与换算（含停顿吸附）、无产物空数组、无段 404、缺身份 422、`_list_offline_steps` 三种情形、storage 模式清单带 `offline_steps` |
| 实机（dev，lifespan off） | 临时存储根铺 30s 可播 fMP4 HLS + 合成分段 / 概率，headless Edge 驱动：两泳道与 00:00–00:30 轴对齐，游标与视频帧同步，选类淡化生效，点泳道跳播误差 < 1ms，切 processed 正常，控制台与网络无报错 |
| 全量 `pytest tests/` | 900 passed（上一笔 887，+13 为新用例） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| admin 触发离线作业 | 仍需登录机器跑 CLI | 另起一批：作业服务 + CLI 严格路由（未配 offline 的 step 不回落 MOCK） |
| 换成不产概率的模型重跑 | 旧 `label_probs.npz` 残留，与新分段并存显示 | Runner 在 `label_probs()` 为 None 时不删旧文件；有需要时改为删 |
| 清单取 `/lab-f3m8/tasks?limit=200` 后前端过滤 | 超过 200 个 task 时更早的离线结果不出现在下拉 | 量上来再给清单加过滤参数 |
| 逐帧 `media_ms_at` 每次重建段起点列表 | 小时级 step（5 万帧 × 360 段）换算到秒级 | 出现长 step 再加批量换算 |
