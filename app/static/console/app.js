// 控制台外壳：侧栏 + 顶栏（面包屑 / 后端健康灯）+ 当前页 + toast / 确认框。
import { catalog, loadCatalog } from './api/catalog.js';
import { backendError, backendOk, health } from './api/http.js';
import { busyJobCount, startJobs } from './api/jobs.js';
import { fmtWall } from './core/format.js';
import { go, route } from './core/router.js';
import { dropToast, ui } from './core/ui.js';
import { LivePage, liveCrumbs } from './pages/live.js';
import { OfflinePage, offlineCrumbs } from './pages/offline.js';
import { RunPage, runCrumbs } from './pages/run.js';
import { SystemPage, systemCrumbs } from './pages/system.js';
import { TasksPage, tasksCrumbs } from './pages/tasks.js';

const { createApp, ref, computed, watch, onMounted } = Vue;

const ICON = {
  tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
  live: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="2" y="5" width="15" height="14" rx="2"/><path d="M17 10l5-3v10l-5-3z"/></svg>',
  run: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M10 8.5v7l5.5-3.5z" fill="currentColor"/></svg>',
  offline: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M4 12h10M4 17h6"/><circle cx="18" cy="16" r="3"/></svg>',
  system: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 12h4l3-7 4 14 3-7h4"/></svg>',
};

// 路由名 → 页面。crumbs(route) 给面包屑；fill = 页面自己管滚动（工作台）。
const PAGES = {
  tasks: { component: TasksPage, crumbs: tasksCrumbs },
  live: { component: LivePage, crumbs: liveCrumbs },
  run: { component: RunPage, crumbs: runCrumbs, fill: true },
  offline: { component: OfflinePage, crumbs: offlineCrumbs },
  system: { component: SystemPage, crumbs: systemCrumbs },
};

const NAV = [
  { key: 'tasks', name: '总览', tip: '任务总览' },
  { key: 'live', name: '实时', tip: '实时监控' },
  { key: 'run', name: '回放', tip: '回放工作台（最近一次打开的 run）' },
  { key: 'offline', name: '分析', tip: '分析作业队列' },
  { key: 'system', name: '运维', tip: '系统运维' },
].map((n) => ({ ...n, icon: ICON[n.key] }));

const Shell = {
  template: `
<div class="app">
  <aside class="side">
    <div class="logo">CS</div>
    <div v-for="n in NAV" :key="n.key" class="nav" :class="{on: page===n.key}" @click="navTo(n.key)" :title="n.tip">
      <span class="ico" v-html="n.icon"></span><span>{{ n.name }}</span>
      <span class="nbadge" v-if="n.key==='offline' && busyJobCount">{{ busyJobCount }}</span>
    </div>
  </aside>
  <div class="main">
    <header class="top">
      <div class="crumb">
        <template v-for="(c,i) in crumbs" :key="i">
          <span v-if="i" class="sep">›</span>
          <a v-if="c.to" @click="go(c.to)">{{ c.t }}</a><b v-else>{{ c.t }}</b>
        </template>
      </div>
      <div class="lights">
        <div class="light" :class="{bad: !backendOk}" @click.stop="popOpen = !popOpen">
          <i class="dot" :class="{bad: !backendOk}"></i>后端{{ backendOk ? '正常' : '异常' }}
          <div class="pop" v-if="popOpen" @click.stop>
            <h4>后端连接</h4>
            <div v-if="!backendOk" style="color:var(--bad);margin-bottom:6px">{{ backendError }}</div>
            <div class="row" v-for="p in health.pollers" :key="p.name">
              <span class="mono">{{ p.name }}</span>
              <span :style="{color: p.fails ? 'var(--bad)' : 'var(--ok)'}">{{ p.fails ? '失败 ×' + p.fails : '每 ' + p.every / 1000 + 's' }}</span>
            </div>
            <div class="sub" style="margin-top:6px">任一轮询连续 3 次失败即变红；恢复后自动转绿。最后成功 {{ fmtWall(health.lastOkAt) }}</div>
          </div>
        </div>
      </div>
    </header>
    <div class="banner" v-if="!backendOk && !fill">后端轮询失败，页面数据停在 {{ fmtWall(health.lastOkAt) }}，以下内容可能过期。</div>
    <div class="banner" v-else-if="catalogError">读取 step 能力表失败（{{ catalogError }}），step 名暂显示编号，稍后自动重试。</div>
    <div class="content" :class="{fill}" @click="popOpen=false">
      <component :is="current.component" />
    </div>
  </div>
</div>
<div class="toasts">
  <div v-for="t in ui.toasts" :key="t.id" class="toast" :class="t.type">
    <span>{{ t.msg }}</span><a v-if="t.action" @click="t.action.fn(); dropToast(t.id)">{{ t.action.label }}</a>
  </div>
</div>
<div class="mask" v-if="ui.modal">
  <div class="modal">
    <h3>{{ ui.modal.title }}</h3>
    <p>{{ ui.modal.body }}</p>
    <div class="acts">
      <button class="btn" @click="ui.modal.cancel()">{{ ui.modal.cancelText }}</button>
      <button class="btn primary" @click="ui.modal.ok()">{{ ui.modal.okText }}</button>
    </div>
  </div>
</div>`,
  setup() {
    const popOpen = ref(false);
    const catalogError = ref('');
    const page = computed(() => (PAGES[route.name] ? route.name : 'tasks'));
    const current = computed(() => PAGES[page.value]);
    const crumbs = computed(() => current.value.crumbs(route));
    const fill = computed(() => !!current.value.fill);
    let lastRun = '/run';
    watch(() => route.name === 'run' && route.parts.length ? route.parts.join('/') : null, (k) => {
      if (k) lastRun = `/run/${k}`;
    }, { immediate: true });

    function navTo(key) {
      go(key === 'run' ? lastRun : `/${key}`);
    }

    async function loadCatalogWithRetry() {
      try {
        await loadCatalog();
        catalogError.value = '';
      } catch (e) {
        catalogError.value = e.message;
        setTimeout(loadCatalogWithRetry, 5000);
      }
    }

    onMounted(() => {
      loadCatalogWithRetry();
      startJobs();
      document.addEventListener('click', () => { popOpen.value = false; });
    });

    return {
      NAV, page, current, crumbs, fill, popOpen, catalog, catalogError, health, backendOk, backendError, busyJobCount, ui,
      navTo, go, dropToast, fmtWall,
    };
  },
};

createApp(Shell).mount('#app');
