// 各 router 的读写函数；契约见 docs/api/。时间单位在 adapters.js 统一成毫秒，这里原样返回。
import { get, post, request } from './http.js';

function playlistUrl(taskId, stepId, runId, track) {
  const q = new URLSearchParams({ step_id: stepId, track, run_id: runId });
  return `/traceback/task/${taskId}/playlist.m3u8?${q}`;
}

export const api = {
  overview: () => get('/admin-f3m8/overview'),
  stages: () => get('/admin-f3m8/stages'),
  offlineJobs: () => get('/admin-f3m8/offline/jobs'),
  labTasks: ({ q, limit, offset }) => get('/lab-f3m8/tasks', { q, limit, offset }),
  labConfig: () => get('/lab-f3m8/config'),
  labHealth: () => get('/lab-f3m8/health'),
  labSubmit: (body) => post('/lab-f3m8/submit', body),

  /** runId 为 null 时后端解析该 step 最新可见 run，响应回显 run_id。 */
  timeline: (taskId, stepId, runId, track = 'raw') =>
    get(`/traceback/task/${taskId}/timeline`, { step_id: stepId, run_id: runId, track }),
  playlistUrl,
  playlistText: (taskId, stepId, runId, track) => request('GET', playlistUrl(taskId, stepId, runId, track)),
  downloadUrl: (taskId, stepId, runId, track) =>
    `/lab-f3m8/download?${new URLSearchParams({ task_id: taskId, step_id: stepId, run_id: runId, track })}`,
};
