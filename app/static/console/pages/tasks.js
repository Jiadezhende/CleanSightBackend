// 任务总览 #/tasks?tab=online|all：只列有录像的任务；点 step 芯片进该 run 的回放工作台。
import { stageOf } from '../api/catalog.js';
import { api } from '../api/endpoints.js';
import { createPoller } from '../api/http.js';
import { jobOf } from '../api/jobs.js';
import { fmtWall } from '../core/format.js';
import { go, replaceQuery, route } from '../core/router.js';
import { toastError } from '../core/ui.js';

const { ref, reactive, computed, watch, onMounted, onBeforeUnmount } = Vue;

const PAGE_SIZE = 50;

const JOB_CHIP = {
  queued: ['queued', '◌ 排队'],
  running: ['running', '◐ 运行'],
  skipped: ['none', '无检测结果'],
  failed: ['failed', '✗ 失败'],
  reclaimed: ['none', '已回收'],
  cancelled: ['none', '已取消'],
};

export const TasksPage = {
  template: `
<div class="page">
  <div class="card">
    <div class="toolbar">
      <div class="seg">
        <button :class="{on: tab==='online'}" @click="setTab('online')">在线 {{ live.list.length }}</button>
        <button :class="{on: tab==='all'}" @click="setTab('all')">全部</button>
      </div>
      <input class="inp" style="width:260px" v-model="q" placeholder="搜索 task_id / 点位 IP / 状态" />
      <span v-if="tab==='all' && all.source==='storage'" class="tag" :class="taskSource==='db' ? 'bad' : 'mute'">
        {{ taskSource==='db' ? '平台库连不上，已退回仅磁盘：点位 IP、状态暂缺' : '仅磁盘模式：点位 IP、状态为空' }}</span>
      <span class="sub" style="margin-left:auto">只列有录像的任务 · 点 step 进入回放 · 共 {{ tab==='all' ? all.total : onlineRows.length }} 条</span>
    </div>
    <table class="tb">
      <thead><tr>
        <th style="width:90px">任务</th><th style="width:140px">点位 IP</th><th style="width:120px">状态</th>
        <th>steps</th><th style="width:160px">更新时间</th>
      </tr></thead>
      <tbody>
        <tr v-for="t in rows" :key="t.taskId">
          <td class="mono"><b>{{ t.taskId }}</b></td>
          <td class="mono">{{ t.ip || '—' }}</td>
          <td><span class="tag" :class="{'tg-run': t.live}"><i v-if="t.live" class="dot live" style="width:6px;height:6px"></i>{{ t.status || '—' }}</span></td>
          <td>
            <div class="chips">
              <div v-for="s in t.steps" :key="s.stepId" class="chip" @click="openStep(t, s)" :title="s.title">
                <span class="sid">{{ s.stepId }}</span><span class="al">{{ s.alias }}</span>
                <span v-if="s.off" class="off" :class="s.off[0]">{{ s.off[1] }}</span>
              </div>
            </div>
          </td>
          <td class="mono sub">{{ fmtWall(t.updatedMs, true) }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="5" class="empty">{{ loading ? '加载中…' : '没有符合条件的任务' }}</td></tr>
      </tbody>
    </table>
    <div class="more" v-if="tab==='all' && all.items.length < all.total">
      <button class="btn sm" :disabled="loading" @click="loadMore">加载更多（已显示 {{ all.items.length }} / {{ all.total }}）</button>
    </div>
  </div>
</div>`,
  setup() {
    const tab = ref(route.query.tab === 'online' ? 'online' : 'all');
    const q = ref('');
    const loading = ref(false);
    const taskSource = ref('db');
    const live = reactive({ list: [] });                       // overview.clients
    const all = reactive({ items: [], total: 0, source: 'db' });  // /lab/tasks 已加载的页

    const liveByTask = computed(() => Object.fromEntries(live.list.map((c) => [c.task_id, c])));

    function offChip(taskId, stepId, runId) {
      if (!stageOf(stepId).has_offline_model) return null;
      const j = jobOf(taskId, stepId, runId);
      if (!j) return null;
      if (j.status === 'completed') {
        return j.alarmCount ? ['viol', `✓${j.segmentCount} ⚠${j.alarmCount}`] : ['done', `✓${j.segmentCount}`];
      }
      return JOB_CHIP[j.status] || ['none', j.status];
    }

    function stepChip(taskId, stepId, runId, hasOffline) {
      const lv = liveByTask.value[taskId];
      const isLive = lv && lv.step_id === stepId;
      const stage = stageOf(stepId);
      let off = isLive ? ['live', '● 直播'] : offChip(taskId, stepId, runId);
      if (!off && stage.has_offline_model) off = hasOffline ? ['done', '✓ 有结果'] : ['none', '未跑'];
      return { stepId, runId, alias: stage.alias, off, live: isLive, title: runId ? `run ${fmtWall(runId, true)}` : '' };
    }

    const allRows = computed(() => all.items.map((t) => ({
      taskId: t.task_id,
      ip: t.source_ip,
      status: all.source === 'storage' ? (liveByTask.value[t.task_id] ? '在线' : null) : t.status,
      live: !!liveByTask.value[t.task_id],
      updatedMs: t.updated_time,
      steps: t.raw_steps.map((s) => stepChip(t.task_id, s, t.run_ids[String(s)], t.offline_steps.includes(s))),
    })));

    const onlineRows = computed(() => {
      const kw = q.value.trim().toLowerCase();
      return live.list
        .filter((c) => !kw || String(c.task_id).includes(kw) || (c.source_ip || '').toLowerCase().includes(kw))
        .map((c) => ({
          taskId: c.task_id, ip: c.source_ip, status: '在线', live: true, updatedMs: null,
          steps: [stepChip(c.task_id, c.step_id, null, false)],
        }));
    });

    const rows = computed(() => (tab.value === 'all' ? allRows.value : onlineRows.value));

    async function loadAll(reset) {
      loading.value = true;
      try {
        const d = await api.labTasks({ q: q.value.trim(), limit: PAGE_SIZE, offset: reset ? 0 : all.items.length });
        all.items = reset ? d.tasks : all.items.concat(d.tasks);
        all.total = d.total;
        all.source = d.source;
      } catch (e) {
        toastError('加载任务清单', e);
      } finally {
        loading.value = false;
      }
    }
    const loadMore = () => loadAll(false);

    let debounce = null;
    watch(q, () => {
      if (tab.value !== 'all') return;
      clearTimeout(debounce);
      debounce = setTimeout(() => loadAll(true), 300);
    });

    function setTab(v) {
      tab.value = v;
      replaceQuery({ tab: v });
      if (v === 'all') loadAll(true);
    }

    function openStep(t, s) {
      if (s.live) return go(`/live/${t.taskId}`);
      go(`/run/${t.taskId}/${s.stepId}/${s.runId ?? 'latest'}`);
    }

    const overview = createPoller('/admin/overview', 3000, async () => {
      live.list = (await api.overview()).clients;
    });

    onMounted(() => {
      overview.start();
      if (tab.value === 'all') loadAll(true);
      api.labConfig().then((c) => { taskSource.value = c.task_source; }).catch(() => {});
    });
    onBeforeUnmount(() => { overview.stop(); clearTimeout(debounce); });

    return { tab, q, loading, taskSource, live, all, rows, onlineRows, setTab, openStep, loadMore, fmtWall };
  },
};

export const tasksCrumbs = () => [{ t: '任务总览' }];
