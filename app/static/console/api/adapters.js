// 后端形状 → 页面形状。秒/毫秒在这里统一成毫秒，页面只见毫秒；文字一律原样保留后端的值。

/** 离线作业（/admin-f3m8/offline/jobs）：*_at 是 epoch 秒。 */
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
