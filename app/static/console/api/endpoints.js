// 各 router 的读写函数；契约见 docs/api/。时间单位在 adapters.js 统一成毫秒，这里原样返回。
import { get, post, put, request } from './http.js';

function playlistUrl(taskId, stepId, runId, track) {
  const q = new URLSearchParams({ step_id: stepId, track, run_id: runId });
  return `/traceback/task/${taskId}/playlist.m3u8?${q}`;
}

export const api = {
  overview: () => get('/admin/overview'),
  metricsJson: () => get('/admin/metrics/json'),
  healthStatus: () => get('/health/status'),
  monitorConfig: () => get('/health/monitor/config'),
  taskLive: () => get('/task/live'),
  taskMessage: (taskId, sinceSeq) => get(`/task/message/${taskId}`, { since_seq: sinceSeq }),
  /** 延迟探测用：任意 GET，不解析业务含义。 */
  raw: (path) => request('GET', path),
  stages: () => get('/admin/stages'),
  offlineJobs: () => get('/admin/offline/jobs'),
  labTasks: ({ q, limit, offset }) => get('/lab/tasks', { q, limit, offset }),
  labConfig: () => get('/lab/config'),
  /** PUT 是整体覆盖 url / project；task_source 传 null 表示不改。 */
  putLabConfig: (body) => put('/lab/config', body),
  labHealth: () => get('/lab/health'),
  labSubmit: (body) => post('/lab/submit', body),
  labelProbs: ({ task_id, step_id, run_id, track }) => post('/lab/label-probs', { task_id, step_id, run_id, track }),
  temporal: ({ task_id, step_id, run_id, track }) => post('/ai/temporal', { task_id, step_id, run_id, track, type: 'segment' }),
  // 过渡期：离线违规入库后改读 /task/{id}/alarms，只改这一处与 adaptViolation
  offlineAlarms: ({ task_id, step_id, run_id, track }) =>
    get(`/admin/offline/alarms/${task_id}/${step_id}`, { run_id, track }),
  submitOfflineJob: (body) => post('/admin/offline/jobs', body),

  /** runId 为 null 时后端解析该 step 最新可见 run，响应回显 run_id。 */
  timeline: (taskId, stepId, runId, track = 'raw') =>
    get(`/traceback/task/${taskId}/timeline`, { step_id: stepId, run_id: runId, track }),
  playlistUrl,
  playlistText: (taskId, stepId, runId, track) => request('GET', playlistUrl(taskId, stepId, runId, track)),
  downloadUrl: (taskId, stepId, runId, track) =>
    `/lab/download?${new URLSearchParams({ task_id: taskId, step_id: stepId, run_id: runId, track })}`,
};
