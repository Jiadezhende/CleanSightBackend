// 后端形状 → 页面形状。秒/毫秒在这里统一成毫秒，页面只见毫秒；文字一律原样保留后端的值。

/** 离线作业（/admin/offline/jobs）：*_at 是 epoch 秒。 */
export function adaptJob(j) {
  const ms = (sec) => (sec == null ? null : Math.round(sec * 1000));
  return {
    taskId: j.task_id,
    stepId: j.step_id,
    runId: j.run_id,
    status: j.status,
    producer: j.producer,
    segmentCount: j.segment_count,
    alarmCount: j.alarm_count,
    message: j.message,
    submittedMs: ms(j.submitted_at),
    startedMs: ms(j.started_at),
    finishedMs: ms(j.finished_at),
  };
}

export const jobKey = (taskId, stepId, runId) => `${taskId}/${stepId}/${runId}`;

/** 作业列表按 run 建索引；同一 run 只留最新提交的那个。 */
export function indexJobs(jobs) {
  const out = {};
  for (const j of jobs) {
    const k = jobKey(j.taskId, j.stepId, j.runId);
    if (!out[k] || out[k].submittedMs < j.submittedMs) out[k] = j;
  }
  return out;
}

/** timeline 的告警事件 → 事件行（与离线违规共用一种形状）。媒体刻度取请求的那条轨（raw）。 */
export function adaptTimelineAlarm(e) {
  return {
    id: `alarm:${e.alarm_id}`,
    source: 'alarm',
    kind: e.alarm_type,
    level: e.severity,
    message: e.message,
    mediaMs: e.media_offset_ms,
    wallMs: e.ts_ms,
  };
}

/** /ai/temporal 的分割段（闭区间，媒体刻度）。 */
export function adaptSegment(it) {
  return { label: it.label, startMs: it.start_media_ms, endMs: it.end_media_ms, conf: it.conf, producer: it.producer };
}

/** 离线规则判定的盘上告警（过渡期端点，入库后只改这里）→ 事件行。整段判定的没有时刻（mediaMs = null）。 */
export function adaptViolation(it, i) {
  return {
    id: `viol:${i}:${it.rule_id}`,
    source: 'viol',
    kind: it.metric,
    level: it.alarm_level,
    message: it.alarm_message,
    mediaMs: it.media_ms,
    wallMs: null,
  };
}

/** timeline 的断流点：媒体轴上宽度为零的位置 + 墙钟空洞两端。 */
export function adaptGap(g) {
  return { mediaMs: g.media_ms, wallFromMs: g.wall_from_ms, wallToMs: g.wall_to_ms, gapMs: g.gap_ms };
}
