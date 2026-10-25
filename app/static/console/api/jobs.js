// 离线作业的全站共享视图：外壳启动时开始轮询，各页只读 jobs / 调 refreshJobs()。
// 作业从排队 / 运行变成结束时弹一条 toast（带「查看结果」），并在列表里高亮几秒。
import { go } from '../core/router.js';
import { toast } from '../core/ui.js';
import { adaptJob, indexJobs, jobKey } from './adapters.js';
import { api } from './endpoints.js';
import { createPoller } from './http.js';

const { reactive, computed } = Vue;

const ACTIVE = ['queued', 'running'];
const FLASH_MS = 2500;

// 作业状态 → [显示名, 标签样式]。状态是离线作业服务的系统枚举，未知值原样显示。
const STATUS = {
  queued: ['排队中', 'q'],
  running: ['运行中', 'busy'],
  completed: ['已完成', 'ok'],
  skipped: ['无检测结果', 'mute'],
  reclaimed: ['已回收', 'mute'],
  failed: ['失败', 'bad'],
  cancelled: ['已取消', 'mute'],
};
export const jobStatusName = (j) => (j ? (STATUS[j.status] || [j.status])[0] : '未跑');
export const jobTagCls = (j) => (j ? (STATUS[j.status] || [0, 'mute'])[1] : 'mute');

export const jobs = reactive({ list: [], byRun: {}, flashUntil: {} });

export const busyJobCount = computed(() => jobs.list.filter((j) => ACTIVE.includes(j.status)).length);

export const jobOf = (taskId, stepId, runId) => jobs.byRun[jobKey(taskId, stepId, runId)] || null;

let seen = null;   // 上一轮各作业状态；首轮只记录不提示

function announceFinished(list) {
  const prev = seen || {};
  const now = {};
  for (const j of list) {
    const k = jobKey(j.taskId, j.stepId, j.runId);
    now[k] = j.status;
    if (seen && ACTIVE.includes(prev[k]) && !ACTIVE.includes(j.status)) {
      jobs.flashUntil[k] = Date.now() + FLASH_MS;
      const what = `任务 ${j.taskId} · step ${j.stepId} 离线推理${jobStatusName(j)}`;
      const detail = j.status === 'completed' ? `：${j.segmentCount} 段${j.alarmCount != null ? ` · 违规 ${j.alarmCount}` : ''}` : (j.message ? `：${j.message}` : '');
      toast(what + detail, j.status === 'completed' ? 'ok' : 'warn',
        { label: '查看结果', fn: () => go(`/run/${j.taskId}/${j.stepId}/${j.runId}?panel=offline`) }, 6000);
    }
  }
  seen = now;
}

const poller = createPoller('/admin-f3m8/offline/jobs', 3000, async () => {
  const d = await api.offlineJobs();
  const list = d.jobs.map(adaptJob);
  announceFinished(list);
  jobs.list = list;
  jobs.byRun = indexJobs(list);
});

export const startJobs = () => poller.start();
export const refreshJobs = () => poller.refresh();
