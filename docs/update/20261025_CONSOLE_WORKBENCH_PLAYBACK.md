# 控制台第 2 批：回放工作台上线，raw / processed 按墙钟对时，时间轴标出断流与告警

> **变更状态**：生效中（2026-10-25）
> **知识库**：待沉淀

## 概述

控制台新增回放工作台 `#/run/{task}/{step}/{run}`：raw / processed 单轨或并排播放，两轨按墙钟对时；时间轴画刻度、断流点、告警标记，右侧列本 run 的平台告警，点击跳到对应时刻。
后端配套两处：浏览器回放清单每段带 `#EXT-X-PROGRAM-DATE-TIME`（段墙钟起点）；`/traceback/.../timeline` 新增 `gaps[]`（每一处断流的媒体断点与墙钟空洞）。

## 变更背景

- **现状 / 痛点**：
  - 旧 lab / admin 页的双轨只能切换查看，且按相同秒数续播；两轨媒体轴原点不同（processed 首段常晚于 raw），断流时墙钟还会跳，切过去落在错误的时刻。
  - 断流位置看不见，只有一句「含信号中断」；送标到提交时才报 `range_gap`。
  - 历史 run 没有回放入口（`/traceback` 只有接口）。
- **触发来源**：控制台改造提案 [20261025_ADMIN_CONSOLE_REDESIGN_PROPOSAL.md](20261025_ADMIN_CONSOLE_REDESIGN_PROPOSAL.md) 第 2 批（B1 对时、B5 断流点 + 工作台播放器 / 时间轴 / 事件面板）。人已定：对时数据走 playlist 的 `PROGRAM-DATE-TIME`（D1 = B），断流判据以后端为准、前端不复刻阈值。
- **承接**：第 1 批（外壳、出网层、任务总览，[20261025_CONSOLE_SHELL_TASKS.md](20261025_CONSOLE_SHELL_TASKS.md)）。

## 方案详情

### 全景

```text
任务总览点 step 芯片 ─▶ #/run/{task}/{step}/{run_id}
  ① GET /traceback/task/{t}/timeline?step_id&track=raw[&run_id]
       → run_id（latest 落成具体值写回 URL）、时长、墙钟跨度、gaps[]（B5）、告警 events
  ② GET playlist.m3u8 track=raw / processed（同一 run_id）
       → 逐段 (PROGRAM-DATE-TIME, EXTINF) → MediaMap：媒体刻度 ↔ 墙钟（B1）
       processed 404 / 503 → 只有 raw，并排与 processed 按钮置灰
  ③ DualPlayer：两个 <video> + hls.js；位置一律用 raw 媒体刻度
       并排：raw 主轨，processed 目标 = procMap.mediaAt(rawMap.wallAt(t))
             null（推理预热 / 断流）→ processed 停住并盖「此刻无标注画面」
             偏差 > 150ms（暂停时 > 40ms）seek，否则微调 playbackRate 追平
  ④ Timeline（canvas）：刻度尺 + 断流标签 / 贯穿红线 + 事件泳道；滚轮缩放、拖动平移、点击定位
  ⑤ 事件面板：timeline.events → 事件行；点击 seek，选中写进 URL（ev=）
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 清单逐段墙钟 | `app/services/utils/vod_playlist.py` `VodEntry.wall_ms`；`app/routers/traceback.py` | §1 |
| timeline 断流点 | `app/services/utils/media_timeline.py` `gaps()`；`app/routers/traceback.py` | §2 |
| 清单解析与换算 | `app/static/console/api/playlist.js` | §3 |
| 双轨播放器 | `app/static/console/components/dual-player.js` | §4 |
| 时间轴 | `app/static/console/components/timeline.js` | §5 |
| 工作台页 | `app/static/console/pages/run.js` | §6 |

### 1. 浏览器清单逐段带墙钟（B1）

`VodEntry` 加可选 `wall_ms`；`render_vod` 遇到它在该段 `#EXTINF` 前写 `#EXT-X-PROGRAM-DATE-TIME:<UTC ISO8601 毫秒>`。只有 `/traceback` 的浏览器清单填（段文件名里的 `ts_ms`），lab 裁剪 / 整段导出喂 ffmpeg 的清单不填，文本不变。

### 2. timeline 回传断流点（B5）

`media_timeline` 把阈值筛选后的空隙迭代器公开为 `gaps(tl)`（`first_gap` / `total_gap_ms` 同用它）；timeline 响应加 `gaps: [{media_ms, wall_from_ms, wall_to_ms, gap_ms}]`，判据与 `gap_total_ms`、送标 `range_gap` 同源。前端不再自己按 500ms 阈值重算。

### 3. 前端对时：自己解析清单文本

前端 `fetch` 一次清单文本解析 `PROGRAM-DATE-TIME` + `EXTINF`，而不是读 hls.js 解析出的 fragment：换算与播放库解耦，原生 HLS 回退时同样可用。hls.js 自己再拉一次清单；两次拉取之间 run 若还在录，后一次只会在尾部多出段，前面的对照不变。媒体轴段起点 = 之前各段 EXTINF 取整到 ms 之和，与后端 `MediaTimeline` 同口径。

### 4. 双轨播放器

- 单轨切换时按墙钟换算目标位置再切，不再按相同秒数续播。
- 段 token 过期（403）或 hls.js 致命网络错误：销毁重建、回到原位置，5 秒内最多一次。

### 5. 时间轴

刻度尺 + 泳道两层；静态层（刻度、断流、泳道内容）只在数据 / 视窗变化时重画，播放头每帧画。泳道以 `{key, name, height, draw, hit, tip, empty}` 描述，后续批次往里加分割段 / 概率 / 送标片段泳道，不改时间轴本身。

### 6. 工作台页

- 页面状态进 URL：`t`（暂停 / 定位时）、`mode`、`ev`；同一 run 内带 `t` / `ev` 跳进来只定位不重载。
- 下载：一个「下载 ▾」菜单直接链到 `/lab-f3m8/download`（该端点不支持 HEAD，未做预检；点击后提示服务端正在拼接）。
- 只有 raw 时并排 / processed 置灰；没有 raw 录像时整页给出原因。

### 保留项（不改动）

- 旧 lab / admin 页原样可用。
- timeline 其余字段、playlist 其余文本不变；不带 `wall_ms` 的消费方输出逐字节不变（`test_utils_vod_playlist` 全文比对仍过）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 双轨 | 只能切换，切过去错位 | 并排按墙钟对时；切换按墙钟换算位置 |
| processed 没画面的区间 | 黑屏或错位 | 盖「此刻无标注画面」并说明原因（推理预热 / 断流） |
| 断流 | 一句「含信号中断」 | 时间轴上逐处标出位置与时长，悬停看墙钟两端 |
| 历史回放 | 无页面 | 任务总览任一 step 一点即开 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_utils_vod_playlist.py`（`wall_ms` → PDT，未给不写） | 通过 |
| `tests/test_traceback_router.py`（playlist 逐段 PDT；timeline `gaps` 有断流 / 无断流） | 通过 |
| 全量 `pytest tests/` | 1232 passed |
| 前端 | 本机假后端（ffmpeg 生成的真实 fMP4 段，raw 第 6 段后断流 23s、processed 晚起 2.4s）+ 无头 Edge：并排播放偏差 −8ms；raw 12.0s ↔ processed 9.6s、raw 41.5s（断流后）↔ processed 39.1s 换算正确；预热区与断流区盖「此刻无标注画面」；跨断流时墙钟跳到空洞之后；切 processed 单轨位置不变。**未在 dev 环境用真实录像（含真实断流）走查** |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 真实录像上 hls.js 对逐段 PDT + 断流清单的行为只在假数据上验过 | 若 hls.js 按 PDT 做对齐，可能在断流处跳段或卡住 | 合入前在 dev 用含断流的真实 run 走查一遍 |
| processed 帧的底图是渲染时刻的最新 raw 帧（KB 记为有意设计） | 并排时 processed 底图比 raw 超前约一个推理延迟，框与 raw 对得上 | 后续在并排模式加一句说明 |
| 事件面板只有平台告警 | 离线违规未接入 | 第 4 批 |
| 送标片段泳道 / 送标面板 | 未接入 | 第 3 批 |
