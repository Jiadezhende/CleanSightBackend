# Lab 送标页支持 raw/processed 双轨查看——processed 只看不送标

> **变更状态**：生效中（2026-06-28）
> **知识库**：待沉淀
>
> 相关：[20260614_LAB_CLIP_TIME_MODEL.md](20260614_LAB_CLIP_TIME_MODEL.md)（lab 选段时间模型）、[../archive/old_architecture/LAB_EXPORT_API.md](../archive/old_architecture/LAB_EXPORT_API.md)（送标导出接口）、[../archive/old_architecture/TRACEBACK_API.md](../archive/old_architecture/TRACEBACK_API.md)（双轨 playlist 数据底座）。

## 概述

- **改了什么**：lab 送标工作台的播放器支持在 raw（原始）与 processed（带模型标注）两条轨之间切换查看；processed 为**只读参考轨**，打点禁用，送标始终裁 raw。
- **为什么改**：操作员需要边看模型标注效果边核对，但标注源必须是无偏置的 raw（避免标注者被模型框带偏）。
- **影响面**：**纯前端单文件** [app/static/lab/index.html](../../app/static/lab/index.html)。后端零改动——`/traceback/task/{id}/playlist.m3u8?step_id=...&track=raw|processed` 两轨早已支持、timeline 轨无关、`/lab-f3m8/submit` 本就只裁 raw。

## 改动详情

> 全部集中在 [app/static/lab/index.html](../../app/static/lab/index.html)，无新依赖（hls.js / Vue / Element Plus 均已加载）。

### 1. 播放器 URL 参数化 + 切轨保位

#### 旧
```js
function attachVideo() {
  ...
  const m3u8 = api(`/traceback/task/${form.task_id}/playlist.m3u8?step_id=${form.step_id}&track=raw`);
```

#### 新
```js
function attachVideo(seekSec = null, resumePlay = false) {
  ...
  const m3u8 = api(`/traceback/task/${form.task_id}/playlist.m3u8?step_id=${form.step_id}&track=${track.value}`);
  // hls MANIFEST_PARSED（原生 HLS 走 loadedmetadata once）回调里 seek 回 seekSec + 按需 play()
```

- 新增 `track = ref('raw')` 状态。
- `timeupdate`/`seeked` 监听抽到 `bindVideoListeners()`，用 `videoListenersBound` 标志只绑一次（切轨复用同一 `<video>` 节点，重复 attach 会叠加监听）；`detachVideo()` 复位该标志。

### 2. 新增 `switchTrack(next)`

- 存当前 `currentTime`/播放态 → 切 processed 前 `fetch` **预检** processed playlist，`!r.ok`（某些 step 只有 raw，命中 404/503）则 `ElMessage.warning('该步骤无 processed 轨，保持 raw')` 并 return（不改 track）→ 通过后换源并 seek 回原位、维持播放态。
- UI 用受控开关：`<el-radio-group :model-value="track" @change="switchTrack">`，预检失败时开关自动回弹。

### 3. processed 下打点禁用

- 「设为起点 / 设为终点 / 加入列表」加 `:disabled="track !== 'raw'"`，并提示「processed 为参考轨，打点请切回 raw」。
- 原因：打点数学 `currentAbsMs = timeline.start_ms + currentTime` 基于 **raw 时基**，且 submit 恒裁 raw；processed 首段可能因推理预热比 raw 晚起，允许在其上打点会引入几百 ms 选区偏移。

### 4. 保留项（不改动）

- `submitClips()` 请求体无 `track` 字段，恒裁 raw——「processed 不送标」由构造保证，未加任何分支。
- timeline / 选区 / 校验：基于绝对墙钟 ms，轨无关，全部未动。
- 后端（traceback / lab 路由、segment_finder）：零改动。

## 数据通道 / 行为说明

| 通道 | 填充 | 消费 | 本次影响 |
|------|------|------|---------|
| `track=raw` playlist | persistence（raw 段） | lab 播放 + 选段 + **送标源** | 否——默认轨，行为不变 |
| `track=processed` playlist | persistence（processed 段） | lab 播放（**仅参考查看**） | 是——新增可切入，但不进送标 |

## 验证

| 项 | 结果 |
|----|------|
| 切 processed | 换源播出带框标注画面、停在同一时刻；打点按钮置灰并提示 |
| 切回 raw | 恢复原始画面，打点按钮恢复 |
| 只有 raw 的 step 切 processed | 弹「该步骤无 processed 轨，保持 raw」，开关回弹 raw |
| raw 选段送标 | 产物仍为 raw 裁剪片（`clips[].success`，LS task 可播），与当前查看轨无关 |
| 后端 / 单测 | 无后端改动，不涉及 pytest |
