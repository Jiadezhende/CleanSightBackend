// 分析作业 #/offline?status=：离线推理作业的队列与历史；行点击进该 run 工作台的离线分析面板。
// 只看不提交：提交入口在工作台「离线分析」面板（后期任务结束自动跑）。
import { stageOf } from '../api/catalog.js';
import { jobs, jobStatusName, jobTagCls } from '../api/jobs.js';
import { fmtDur, fmtWall } from '../core/format.js';
import { go, replaceQuery, route } from '../core/router.js';

const { ref, computed, onMounted, onBeforeUnmount } = Vue;

const FILTERS = [
  { k: 'all', n: '全部' },
  { k: 'running', n: '运行中' },
  { k: 'queued', n: '排队' },
  { k: 'completed', n: '已完成' },
  { k: 'failed', n: '失败' },
];

export const OfflinePage = {
  template: `
<div class="page">
  <div class="page-h"><h2>分析作业</h2><span class="sub">离线推理在回放工作台的「离线分析」里提交；本页看队列与历史（进程重启后清空）</span></div>
  <div class="card">
    <div class="toolbar">
      <div class="seg">
        <button v-for="f in FILTERS" :key="f.k" :class="{on: flt===f.k}" @click="setFilter(f.k)">{{ f.n }} {{ countOf(f.k) }}</button>
      </div>
      <span class="sub" style="margin-left:auto">行点击进入该 run 的离线分析</span>
    </div>
    <table class="tb">
      <thead><tr><th>任务</th><th>step</th><th>run</th><th>状态</th><th>模型</th><th>段</th><th>违规</th><th>耗时</th><th>提交时间</th><th>说明</th></tr></thead>
      <tbody>
        <tr v-for="j in rows" :key="j.taskId + '/' + j.stepId + '/' + j.runId + '/' + j.submittedMs" class="click"
            :class="{flash: (jobs.flashUntil[j.taskId + '/' + j.stepId + '/' + j.runId] || 0) > now}"
            @click="go('/run/' + j.taskId + '/' + j.stepId + '/' + j.runId + '?panel=offline')">
          <td class="mono"><b>{{ j.taskId }}</b></td>
          <td>{{ j.stepId }} {{ stageOf(j.stepId).alias }}</td>
          <td class="mono sub">{{ fmtWall(j.runId, true) }}</td>
          <td><span class="tag" :class="jobTagCls(j)">{{ jobStatusName(j) }}</span></td>
          <td class="mono sub">{{ j.producer || '—' }}</td>
          <td class="mono">{{ j.status === 'completed' ? j.segmentCount : '—' }}</td>
          <td class="mono"><span :style="{color: j.alarmCount ? 'var(--warn)' : ''}">{{ j.alarmCount ?? '—' }}</span></td>
          <td class="mono">{{ elapsed(j) }}</td>
          <td class="mono sub">{{ fmtWall(j.submittedMs, true) }}</td>
          <td class="sub">{{ j.message }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="10" class="empty">没有此状态的作业</td></tr>
      </tbody>
    </table>
  </div>
</div>`,
  setup() {
    const flt = ref(FILTERS.some((f) => f.k === route.query.status) ? route.query.status : 'all');
    const now = ref(Date.now());
    const timer = setInterval(() => { now.value = Date.now(); }, 1000);
    onBeforeUnmount(() => clearInterval(timer));
    onMounted(() => { now.value = Date.now(); });

    const sorted = computed(() => [...jobs.list].sort((a, b) => b.submittedMs - a.submittedMs));
    const rows = computed(() => (flt.value === 'all' ? sorted.value : sorted.value.filter((j) => j.status === flt.value)));
    const countOf = (k) => (k === 'all' ? jobs.list.length : jobs.list.filter((j) => j.status === k).length);
    function setFilter(k) { flt.value = k; replaceQuery({ status: k === 'all' ? null : k }); }
    function elapsed(j) {
      if (j.status === 'running' && j.startedMs) return fmtDur(now.value - j.startedMs);
      if (j.finishedMs && j.startedMs) return fmtDur(j.finishedMs - j.startedMs);
      return '—';
    }

    return { FILTERS, flt, rows, jobs, now, countOf, setFilter, elapsed, go, stageOf, fmtWall, jobStatusName, jobTagCls };
  },
};

export const offlineCrumbs = () => [{ t: '分析作业' }];
