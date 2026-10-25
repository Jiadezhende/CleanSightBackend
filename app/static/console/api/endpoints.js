// 各 router 的读写函数；契约见 docs/api/。时间单位在 adapters.js 统一成毫秒，这里原样返回。
import { get } from './http.js';

export const api = {
  overview: () => get('/admin-f3m8/overview'),
  stages: () => get('/admin-f3m8/stages'),
  offlineJobs: () => get('/admin-f3m8/offline/jobs'),
  labTasks: ({ q, limit, offset }) => get('/lab-f3m8/tasks', { q, limit, offset }),
  labConfig: () => get('/lab-f3m8/config'),
};
