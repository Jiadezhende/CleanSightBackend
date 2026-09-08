# 告警证据反查下线 —— 两个 `/traceback/alarm/*` 端点删除

- **日期**：2026-09-08
- **状态**：待沉淀
- **影响面**：对外契约（删 2 个公开端点）、`step_store` 读接口、admin/lab 前端、配置项

---

## 结论

「从 `alarm_id` 反查视频段」整条能力下线：`/traceback/alarm/{id}/evidence` 与
`/traceback/alarm/{id}/playlist.m3u8` 两个端点删除，`Step.segments_around` 随之删除。

**不丢能力**：同一需求 lab 页面早已用更好的方式实现了一遍 —— `timeline` 出告警打点的
`ts_ms`，前端在整 step 回放上 seek 过去。那是**帧级**定位，旧实现是**段级**（跳到告警
所在那个 ~10s 段的起点）。admin 告警列表的「证据」按钮改名「录像」，跳 lab 深链。

---

## 1. 为什么是过度设计

判断依据不是"用得少"，是**这套东西已经在烂，且烂了没人发现** —— 说明没有真实消费方：

| 症状 | 证据 |
|------|------|
| `clips[].url` 无人消费 | 前端两个页面都不读；KB 明写「裸 fMP4 fragment，不能直接喂 `<video>`」；唯一使用者是集成测试 T4 —— 测试在验证一个只有它自己用的字段 |
| 文档自相矛盾 | `traceback.md` 字段表写 `[].url`「token 化绝对地址，**直接可播**」，同页往下四行的前端坑点写「fMP4 裸段浏览器**解不了**（缺 init）」 |
| 前端两处死绑定 | `evidence.client_id`、`evidence.detection`（keypoints JSON 面板）后端**都不返回**，[20260701 快照](20260701_TRACEBACK_CAPABILITY_SNAPSHOT.md) 已记「不返回」 |
| 自动 seek 位置算错 | 见 §2 |

### 与 lab 现有实现的重复

```
旧：alarm_id → /evidence（段列表 + is_trigger）
            → /alarm/{id}/playlist.m3u8（触发段 ± 上下文）
            → 前端按 ts_ms 差算偏移，seek 到触发段起点          段级

新：task_id + step_id → /task/{id}/timeline（events[].ts_ms）
                      → /task/{id}/playlist.m3u8（整 step）
                      → seekToMs(ev.ts_ms)                     帧级
```

后者不需要额外端点、不需要 `segments_around`、不需要段列表出 `step_store`。

## 2. 顺带修掉的一个真 bug（P1）

两个端点对**同一个告警**返回的段集合不一致：

```
/evidence                segments_around()  → 刻意不滤在途段      store.py:298（原）
/alarm/{id}/playlist     segments_around()  → vod_playlist 再滤一道  store.py:356
```

前端拿**未过滤**的列表算下标与 seek 偏移（`(clips[i].ts_ms - clips[0].ts_ms)/1000`），
喂给**已过滤**的流。窗口内只要有一个在途段，偏移整体偏掉约一个段长（~10s）。

触发条件正是最常用的场景：**看实时任务的新告警**（末段仍在 transcode）。

新链路无此问题：timeline 与 playlist 都只认已入 playlist 的段，口径一致。

---

## 3. 改动清单

### 后端

| 文件 | 改动 |
|------|------|
| `app/routers/traceback.py` | 删 `get_alarm_evidence` / `get_alarm_evidence_playlist` / `_segment_to_url` / `_clips` / `_fetch_alarm`；`_build_vod_playlist` 去掉 `segs` 参数；接口重编号（1=playlist, 2=timeline）；`SegmentRef` 不再导入 |
| `app/services/step_store/store.py` | 删 `Step.segments_around`；`_locate_containing_index` 搬去 `segment_decoder`（见下）；`bisect` 导入删除；`vod_playlist` 的 `segments` 参数 docstring 改写 |
| `app/services/step_store/segment_decoder.py` | 接收 `_locate_containing_index` —— 它原在 `store.py`，`segments_around` 删除后**零本模块调用方**，只剩本模块 3 处使用，且「两端对 -1 处理相反」的论证整个落在 `iter()` 里。顺带消掉一处跨模块私有名导入 |
| `app/services/step_store/layout.py` | 注释指向改 `segment_decoder._locate_containing_index` |
| `app/services/inference/offline/frame_finder.py` | 删「与 `Step.segments_around` 成对仗」一句 |
| `app/settings.py` | 删 `traceback_context_before` / `traceback_context_after`（失去全部调用方） |

**`/task/{id}/playlist.m3u8` 的空判顺带收口**：原先取全量段列表只为判空、再原样传回
`vod_playlist`（包内重新 join 一遍时长）。改判 `track not in step.tracks` —— 与
`segments(track, playable_only=False)` 判空等价、同走一次目录扫描缓存，段列表不再出包。
至此 `SegmentRef` 在 `app/routers/` 下**零出现**。

### 前端

| 文件 | 改动 |
|------|------|
| `app/static/admin/index.html` | 删证据弹窗（模板 52 行 + 脚本 111 行：`loadEvidencePlaylist` / `seekToClip` / `clipOffsetSec` / `onTrackChange` / hls 实例管理 / 8 个 ref）；「证据」按钮 → 「录像」，`viewRecording(row)` 开 lab 深链 |
| `app/static/lab/index.html` | 新增 `applyDeepLink()`：`?task_id=&step_id=[&alarm_id=]`，`onMounted` 里调用 |

**深链传 `alarm_id` 而非时间戳**：`/task/{id}/alarms` 的 `detected_at` 是 **DB 原值**
（`task.py:67` 直接 `int(r.detected_at)`，未归一），而 timeline 的 `events[].ts_ms` 走过
`_to_ms`。跨页传原值等于把归一化责任摊给每个调用方。lab 收到 `alarm_id` 后从自己
已归一化的 timeline events 里取 `ts_ms`，单一真源。

> **遗留（未修，需决策）**：`/task/{id}/alarms` 的 `detected_at` 未归一化，但
> [task.md](../api/task.md) 的字段表写的是「epoch **毫秒**」。同一个 DB 列，timeline 归一、
> alarms 不归一。当前实践中 DB 存的是毫秒（否则 admin 的 `formatTs` 早就显示 1970），
> 故未触发。要么让代码符合文档（改 `task.py` 加 `_to_ms`，但会改变对外取值），要么改文档
> 承认返回原值。**本次未动，因为它超出下线范围且是独立的契约问题。**

### 测试

| 文件 | 改动 |
|------|------|
| `tests/test_traceback_router.py` | 删 `_patch_alarm_lookup` + 5 个 evidence 用例；**新增** `test_timeline_normalizes_detected_at_unit`（参数化秒/毫秒/微秒）—— 原 `test_evidence_handles_seconds_unit` 是 `_to_ms` 的唯一覆盖，而 `_to_ms` 仍在给 timeline 用，不能随端点一起掉 |
| `tests/test_step_store_api.py` | 删 `TestSegmentsAround` 整类（8 个用例） |
| `integration_tests/test_traceback.py` | 删 T1 evidence；T4 原本跟进 `raw_clips[trigger].url`（即将删除的字段），改成跟进 T1 playlist 里的段 URI —— **更贴真实播放路径**；T2/T3/T4 顺次重编号为 T1/T2/T3 |

`pytest tests/` **509 passed**（改动前后同数：删 13 个、加 3 个参数化用例，其余单纯计数巧合，
非"测试没跑到"）。

### 文档

| 文件 | 改动 |
|------|------|
| `docs/api/traceback.md` | 标题改「步骤回放与时间轴」；删两个端点整节；顶部加下线说明；删 `n_before`/`n_after` 全局约定；静默失败表里两条 `/evidence` 条目替换 |
| `docs/api/README.md`、`README.md` | 端点索引改写 |

---

## 4. 待沉淀到 KB

`docs/kb/SERVICE_TRACEBACK_MEDIA.md` 与 `BUSINESS_TRACEBACK_AND_LAB.md` 仍描述
evidence 端点、`*_clips[]` 结构、`traceback_context_*` 配置项，**本次按规范未动**
（KB 只在人发起维护流程时更新）。融合时需一并处理，包括删掉那句
「`*_clips[].url` 是裸 fMP4 fragment…」—— 该警告随字段一起消失。

`docs/update/20260906_STEP_STORE_EXTRACTION.md` §2.3 / §5 里关于 `segments_around` 与
`SegmentRef.is_trigger` 的论证是**历史记录，不改**；但其结论（「`segments_around` 刻意不滤
在途段」）已随方法删除而失效，融合时不要再采信。
