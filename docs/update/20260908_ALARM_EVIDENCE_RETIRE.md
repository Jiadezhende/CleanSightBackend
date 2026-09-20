# 告警证据反查下线（2026-09-08）

> **知识库**：已沉淀 → [SERVICE_TRACEBACK_MEDIA.md](../kb/SERVICE_TRACEBACK_MEDIA.md)、[BUSINESS_TRACEBACK_AND_LAB.md](../kb/BUSINESS_TRACEBACK_AND_LAB.md)、[BUSINESS_OVERVIEW.md](../kb/BUSINESS_OVERVIEW.md)、[ARCHITECTURE_API_SURFACE.md](../kb/ARCHITECTURE_API_SURFACE.md)（2026-09-20）

`GET /traceback/alarm/{alarm_id}/evidence` 与 `GET /traceback/alarm/{alarm_id}/playlist.m3u8` 两个公开端点**删除**，连同只服务它们的下游代码、admin 面板的证据 UI 一并清除。`/traceback/task/{task_id}/playlist.m3u8` 与 `/traceback/task/{task_id}/timeline` **保留不动**（lab 页面在用）。

## 为什么下线：这套东西已经在烂，且烂了没人发现

1. **前端两处死绑定**。admin 证据弹窗渲染 `evidence.client_id` 与 `evidence.detection`（含「查看 Keypoints JSON」按钮），后端返回体里**这两个字段都不存在**——只有 `alarm` / `task_id` / `step_id` / `raw_clips` / `processed_clips`。`detection` 是 [20260627_DROP_DEAD_KEYPOINTS_LANDING.md](20260627_DROP_DEAD_KEYPOINTS_LANDING.md) 下线 keypoints 时从后端摘掉的、前端没跟；`client_id` 更早就没了。UI 表现为「客户端：」后面永远空白、Keypoints 按钮永远不出现，从没人报过。
2. **文档自相矛盾**。`docs/api/traceback.md` 一处写 alarm playlist「供 **admin / lab** 端直接播放」，另一处又把 lab 前端列为本组接口的参考实现——而 lab 页面从头到尾只调 `task/{id}/playlist.m3u8` 和 `timeline`，**从没碰过 alarm 系端点**。
3. **同一需求 lab 已实现得更好**。lab 走 task 级 VOD + timeline 打点 + 帧级 seek，可连续拖动整个 step；alarm evidence 只能给「触发段 ± N 段」的粗切片，还要靠 `(clips[i].ts_ms - clips[0].ts_ms)/1000` 手算 offset 去 seek。

## 删了什么

**后端**

- `app/routers/traceback.py`：`get_alarm_evidence`、`get_alarm_evidence_playlist` 两个 handler，及只服务它们的 `_fetch_alarm`、`_segment_to_url`。`_to_ms` / `_fetch_task_alarms` 保留（timeline 在用）；`_build_vod_playlist` 保留，现在只剩 `get_task_playlist` 一个调用方。顺手删掉一个早已无人使用的 `from pathlib import Path`。
- `app/services/traceback/segment_finder.py`：`SegmentFinder.find()`（二分定位 + 上下文扩展）、`SegmentRef.is_trigger` 字段、随之无用的 `import bisect`。全仓仅上述两个端点消费过它们；lab 用的是另一套 `app/services/inference/offline/frame_tracker.py` 的 `FrameTracker.find`，不受影响。
- `app/settings.py`：`traceback_context_before` / `traceback_context_after`（对应环境变量 `CLEANSIGHT_TRACEBACK_CONTEXT_BEFORE` / `_AFTER`，此后无效）。

**前端**

- `app/static/admin/index.html`：告警列表的「操作」列（证据按钮）、证据弹窗、setup 里整段证据逻辑（约 110 行）与其导出项。
- `app/static/admin/vendor/hls.js`（530KB）与其 `<script>` 标签——证据播放是 admin 页面唯一的 hls.js 使用者。lab 有自己的 `app/static/lab/vendor/hls.js`，不受影响。

**测试**

- `tests/test_traceback_router.py`：5 个 evidence 用例 + `_patch_alarm_lookup`。
- `tests/test_traceback_segment_finder.py`：`TestFind` 整个类（7 个用例）。
- `integration_tests/test_traceback.py`：原 T1（evidence）删除；原 T4（媒体段可达）改为从 T1 playlist 正文里取第一条段 URL，覆盖不丢。测试项重编号为 T1 playlist / T2 timeline / T3 media_seg。

回归：`pytest tests/` 439 passed。

## 顺带修掉的两处集成测试腐坏

改 `integration_tests/test_traceback.py` 时发现它其实一直跑不过，两个 bug 都与本次下线无关、但同属「烂了没人发现」：

1. **playlist 用例必然 503**：`integration_tests/utils.py` 的 `seed_hls_segments` 只造段文件和 playlist，**不造 `{track}_init.mp4`**。而 `_build_vod_playlist` 在缺 init 时直接抛 503（fMP4 无 init 段无法解码）。已补上两轨的 init 哑文件。
2. **段数断言口径错**：段行过滤条件是 `l.endswith(".mp4") or "/media/" in l`，会把 `#EXT-X-MAP:URI="http://host/media/init/<token>"` 那行也数进去，实际段数永远比预期多 1。已改为「非 `#` 开头且含 `/media/segment/`」。

## KB 需要改的位置（已于 2026-09-20 按此沉淀）

按 CLAUDE.md，本次不动 `docs/kb/`。下次融合时以下位置要跟：

- `docs/kb/BUSINESS_TRACEBACK_AND_LAB.md`：「告警证据回溯」整节（入口两条、流程 6 步、`traceback_context_*` 两个配置项）删除。
- `docs/kb/SERVICE_TRACEBACK_MEDIA.md`：小标题「/traceback/*（业务层，**4 个**端点）」改 2 个；端点表里 `/alarm/{alarm_id}/evidence`、`/alarm/{alarm_id}/playlist.m3u8` 两行删除；「两个 `playlist.m3u8` 端点以 `api_route` 注册」改为一个；「`evidence` 是双轨能力唯一的并列出口……（`_segment_to_url`，traceback.py:61）」整段删除——`_segment_to_url` 已随端点删除，双轨并列出口不复存在（现在两轨各自是独立 playlist）；「告警双轨复核」那条消费路径删除。
- `docs/kb/BUSINESS_OVERVIEW.md`：「告警证据回溯：`GET /traceback/alarm/{alarm_id}/evidence` 和对应 playlist」一句删除。
- `docs/kb/TESTING_MAP.md`：`tests/test_traceback_router.py` / `test_traceback_segment_finder.py` 的覆盖描述与 2026-07-05 的覆盖率快照均已过时（用例数变化）。

已同步改完的文档：`docs/api/traceback.md`（删两节 + 交叉引用，标题改为「任务回放与时间轴」）、`docs/api/README.md`、`README.md`。`docs/archive/` 下的历史文档按惯例不动。
