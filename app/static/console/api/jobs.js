// 离线作业的全站共享视图：外壳启动时开始轮询，各页只读 jobs / 调 refreshJobs()。
import { adaptJob, indexJobs, jobKey } from './adapters.js';
import { api } from './endpoints.js';
import { createPoller } from './http.js';

const { reactive, computed } = Vue;

export const jobs = reactive({ list: [], byRun: {} });

export const busyJobCount = computed(
  () => jobs.list.filter((j) => j.status === 'queued' || j.status === 'running').length,
);

export const jobOf = (taskId, stepId, runId) => jobs.byRun[jobKey(taskId, stepId, runId)] || null;

const poller = createPoller('/admin-f3m8/offline/jobs', 3000, async () => {
  const d = await api.offlineJobs();
  jobs.list = d.jobs.map(adaptJob);
  jobs.byRun = indexJobs(jobs.list);
});

export const startJobs = () => poller.start();
export const refreshJobs = () => poller.refresh();
