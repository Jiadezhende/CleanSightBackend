// 系统运维 #/system：推理延迟（按模型）、核心计数器、健康监控、各路队列深度、延迟探测、LS 配置、任务清单来源。
// 指标名、字段名一律原样显示后端给的键。
import { api } from '../api/endpoints.js';
import { createPoller } from '../api/http.js';
import { fmtWall } from '../core/format.js';
import { labelColor } from '../core/levels.js';
import { toast, toastError } from '../core/ui.js';

const { ref, reactive, computed, onMounted, onBeforeUnmount } = Vue;

const METRICS_MS = 5000;
const HISTORY = 60;                         // 延迟曲线保留的采样点数（5 分钟）
const COUNTERS = [                           // /metrics/json 的计数器族 → 明细字段
  ['infer_failure_total', 'by_type'],
  ['frame_drop_total', 'by_reason'],
  ['retry_total', 'by_operation'],
];
const PROBE_ENDPOINTS = ['/admin/ping', '/health/status', '/admin/overview', '/task/live'];
const CHART = { w: 640, h: 210, left: 40, right: 8, top: 14, bottom: 22 };

export const SystemPage = {
  template: `
<div class="page">
  <div class="page-h"><h2>系统运维</h2><span class="sub">指标 · 健康监控 · 队列 · 延迟探测 · 配置</span></div>
  <div class="grid2">
    <div class="card">
      <div class="card-h"><h3>推理延迟（按模型）</h3>
        <div class="seg" style="margin-left:auto"><button v-for="q in ['p50', 'p95', 'p99']" :key="q" :class="{on: quant === q}" @click="quant = q">{{ q }}</button></div>
      </div>
      <div class="card-b">
        <div class="lgd">
          <span v-for="m in models" :key="m" :class="{off: hidden.has(m)}" @click="toggleModel(m)"><i :style="{background: color(m)}"></i>{{ m }}</span>
          <span class="sub" style="margin-left:auto;cursor:default">/admin/metrics/json · 5s · 页面不可见期间不采样（曲线断开）</span>
        </div>
        <svg :viewBox="'0 0 ' + CHART.w + ' ' + CHART.h" style="width:100%;height:auto;display:block">
          <g font-size="10" fill="#9ca3af" font-family="Consolas,monospace">
            <template v-for="v in yTicks" :key="v">
              <line :x1="CHART.left" :x2="CHART.w - CHART.right" :y1="y(v)" :y2="y(v)" stroke="#eef0f3" />
              <text :x="CHART.left - 6" :y="y(v) + 3" text-anchor="end">{{ v }}</text>
            </template>
            <text :x="CHART.left" :y="CHART.h - 6">{{ history.length ? fmtWall(history[0].at) : '' }}</text>
            <text :x="CHART.w - CHART.right" :y="CHART.h - 6" text-anchor="end">{{ history.length ? fmtWall(history[history.length - 1].at) : '' }}</text>
            <text x="4" y="10">ms</text>
          </g>
          <path v-for="m in models" :key="m" v-show="!hidden.has(m)" :d="pathOf(m)" fill="none" :stroke="color(m)" stroke-width="1.8" />
        </svg>
        <div v-if="!models.length" class="sub">暂无推理延迟数据（指标从未记录过）</div>
      </div>
    </div>
    <div class="card">
      <div class="card-h"><h3>核心计数器</h3><span class="sub">进程启动以来累计</span></div>
      <table class="tb">
        <thead><tr><th>指标</th><th>总数</th><th>明细</th></tr></thead>
        <tbody>
          <tr v-for="c in counters" :key="c.name">
            <td class="mono">{{ c.name }}</td>
            <td class="mono">{{ c.total ?? '未记录' }}</td>
            <td class="sub mono">{{ c.detail }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-h"><h3>健康监控</h3><span class="sub">/health/status · /health/monitor/config</span></div>
      <div class="card-b">
        <div v-if="health.status && health.status !== 'running'" class="sub">健康监控未就绪（{{ health.status }}）</div>
        <div class="tiles">
          <div class="tile" v-for="(v, k) in health.clients" :key="'c' + k"><div class="k mono">{{ k }}</div><div class="v">{{ v }}</div></div>
          <div class="tile" v-for="(v, k) in monitorNumbers" :key="'m' + k"><div class="k mono">{{ k }}</div><div class="v">{{ v }}</div></div>
        </div>
        <div class="sub" style="margin:10px 0 4px" v-if="reconnecting.length">重连中：<span class="mono">{{ reconnecting.join(', ') }}</span></div>
        <div class="sub" style="margin:12px 0 6px">监控配置（秒）</div>
        <div class="chips"><span class="tag" v-for="(v, k) in monitorConfig" :key="k"><span class="mono">{{ k }}</span> {{ v }}</span></div>
      </div>
    </div>
    <div class="card">
      <div class="card-h"><h3>各路队列深度</h3><span class="sub">/admin/overview · 3s · 当前值（帧）</span></div>
      <table class="tb">
        <thead><tr><th>任务</th><th>step</th><th>点位</th><th v-for="k in queueKeys" :key="k" class="mono">{{ k }}</th></tr></thead>
        <tbody>
          <tr v-for="c in queues" :key="c.task_id">
            <td class="mono"><b>{{ c.task_id }}</b></td><td>{{ c.step_id }}</td><td class="mono sub">{{ c.source_ip }}</td>
            <td v-for="k in queueKeys" :key="k" class="mono">{{ c.queue_depths[k] }}</td>
          </tr>
          <tr v-if="!queues.length"><td :colspan="3 + queueKeys.length" class="empty">没有在线 run</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-h"><h3>延迟探测</h3></div>
      <div class="card-b">
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <select class="inp" v-model="probe.ep"><option v-for="e in PROBE_ENDPOINTS" :key="e">{{ e }}</option></select>
          <span class="sub">次数</span><input class="inp mono" style="width:70px" type="number" min="1" max="200" v-model.number="probe.n">
          <span class="sub">并发</span><input class="inp mono" style="width:60px" type="number" min="1" max="10" v-model.number="probe.c">
          <button class="btn primary" :disabled="probe.busy" @click="runProbe">{{ probe.busy ? '测试中…' : '开始' }}</button>
        </div>
        <template v-if="probe.res">
          <div class="tiles" style="margin-top:12px;grid-template-columns:repeat(6,1fr)">
            <div class="tile" v-for="(v, k) in probe.res.stat" :key="k"><div class="k">{{ k }}</div><div class="v" style="font-size:15px">{{ v }}</div></div>
          </div>
          <div class="hist"><i v-for="(h, i) in probe.res.bins" :key="i" :style="{height: (h / probe.res.maxBin * 100) + '%'}" :title="h"></i></div>
          <div class="sub mono" style="display:flex;justify-content:space-between"><span>{{ probe.res.lo }} ms</span><span>{{ probe.res.hi }} ms</span></div>
        </template>
      </div>
    </div>
    <div class="card">
      <div class="card-h"><h3>Label Studio 配置</h3><span class="sub">写入 lab_runtime_config.json</span></div>
      <div class="card-b">
        <div class="form">
          <span class="sub">LS 地址</span><input class="inp mono" v-model="cfg.url" placeholder="http://… 留空 = 不送标">
          <span class="sub">默认 project</span><input class="inp mono" style="width:120px" type="number" min="0" v-model.number="cfg.pid">
          <span class="sub">Token</span><span class="sub">{{ cfg.tokenConfigured ? '已配置' : '未配置' }}（只能在后端 .env 设置）</span>
          <span></span><div><button class="btn primary" :disabled="cfg.saving" @click="saveLs">保存</button></div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-h"><h3>任务清单来源</h3><span class="sub">决定任务总览「全部」列哪些任务</span></div>
      <div class="card-b">
        <label class="opt" :class="{on: cfg.src === 'db'}"><input type="radio" value="db" v-model="cfg.src">
          <div><b>磁盘 + 平台库</b> <span class="sub">默认</span>
            <div class="sub">以磁盘上有录像的任务为准，再用平台库过滤一遍，并补全点位 IP、状态。平台库连不上时自动退回「仅磁盘」。</div></div></label>
        <label class="opt" :class="{on: cfg.src === 'storage'}"><input type="radio" value="storage" v-model="cfg.src">
          <div><b>仅磁盘</b>
            <div class="sub">不查平台库，列出磁盘上所有有录像的任务；点位 IP、状态为空。用于没有平台库的环境。</div></div></label>
        <div style="display:flex;align-items:center;gap:10px;margin-top:10px">
          <span class="sub">当前生效</span>
          <span class="tag" :class="effTag.cls">{{ effTag.text }}</span>
          <button class="btn primary" style="margin-left:auto" :disabled="cfg.saving" @click="saveSource">保存</button>
        </div>
      </div>
    </div>
  </div>
</div>`,
  setup() {
    const history = ref([]);                 // [{ at, latency: { model: {p50,p95,p99} } }]
    const lastMetrics = ref({});
    const quant = ref('p95');
    const hidden = reactive(new Set());
    const health = reactive({ status: null, clients: {}, monitor: {} });
    const monitorConfig = ref({});
    const queues = ref([]);
    const probe = reactive({ ep: PROBE_ENDPOINTS[0], n: 50, c: 5, busy: false, res: null });
    const cfg = reactive({ url: '', pid: 0, src: 'db', savedSrc: 'db', tokenConfigured: false, saving: false, effective: null });

    const models = computed(() => {
      const s = new Set();
      for (const h of history.value) Object.keys(h.latency).forEach((m) => s.add(m));
      return [...s].sort();
    });
    const color = (m) => labelColor(models.value, m);
    const maxY = computed(() => {
      let mx = 0;
      for (const h of history.value) for (const v of Object.values(h.latency)) mx = Math.max(mx, v[quant.value] || 0);
      return Math.max(10, Math.ceil(mx / 10) * 10);
    });
    const yTicks = computed(() => [0, 0.25, 0.5, 0.75, 1].map((r) => Math.round(maxY.value * r)));
    const y = (v) => CHART.h - CHART.bottom - (v / maxY.value) * (CHART.h - CHART.top - CHART.bottom);
    function pathOf(m) {
      const pts = history.value;
      if (!pts.length) return '';
      const t0 = pts[0].at;
      const span = Math.max(1, pts[pts.length - 1].at - t0);
      let d = '';
      let pen = false;
      pts.forEach((h, i) => {
        const v = h.latency[m];
        const gap = i > 0 && h.at - pts[i - 1].at > METRICS_MS * 2.5;   // 页面不可见期间没采样：断开
        if (!v || gap) pen = false;
        if (!v) return;
        const x = CHART.left + (h.at - t0) / span * (CHART.w - CHART.left - CHART.right);
        d += `${pen ? 'L' : 'M'}${x.toFixed(1)},${y(v[quant.value]).toFixed(1)}`;
        pen = true;
      });
      return d;
    }
    const toggleModel = (m) => (hidden.has(m) ? hidden.delete(m) : hidden.add(m));

    const counters = computed(() => COUNTERS.map(([name, by]) => {
      const c = lastMetrics.value[name];
      return {
        name,
        total: c ? c.total : null,
        detail: c ? Object.entries(c[by] || {}).map(([k, v]) => `${k}:${v}`).join(' · ') : '',
      };
    }));
    const monitorNumbers = computed(() => Object.fromEntries(Object.entries(health.monitor).filter(([, v]) => typeof v === 'number')));
    const reconnecting = computed(() => health.monitor.reconnecting_clients || []);
    const queueKeys = computed(() => {
      const first = queues.value[0];
      return first ? Object.keys(first.queue_depths).filter((k) => typeof first.queue_depths[k] === 'number') : [];
    });
    const effTag = computed(() => {
      if (cfg.savedSrc === 'storage') return { cls: 'mute', text: '仅磁盘' };
      if (cfg.effective === 'storage') return { cls: 'bad', text: '平台库连不上，已退回仅磁盘' };
      return cfg.effective === 'db' ? { cls: 'ok', text: '磁盘 + 平台库' } : { cls: 'mute', text: '检查中…' };
    });

    const metrics = createPoller('/admin/metrics/json', METRICS_MS, async () => {
      const d = await api.metricsJson();
      lastMetrics.value = d;
      history.value = [...history.value, { at: Date.now(), latency: d.infer_latency_ms || {} }].slice(-HISTORY);
    });
    const healthPoll = createPoller('/health/status', 5000, async () => {
      const d = await api.healthStatus();
      health.status = d.status;
      health.clients = d.clients || {};
      health.monitor = d.monitor_stats || {};
    });
    const overview = createPoller('/admin/overview', 3000, async () => { queues.value = (await api.overview()).clients; });

    async function loadConfig() {
      try {
        const c = await api.labConfig();
        Object.assign(cfg, { url: c.label_studio_url, pid: c.default_project_id, src: c.task_source, savedSrc: c.task_source, tokenConfigured: c.token_configured });
        cfg.effective = (await api.labTasks({ limit: 1, offset: 0 })).source;
      } catch (e) {
        toastError('读取配置', e);
      }
    }
    async function putConfig(taskSource) {
      cfg.saving = true;
      try {
        const c = await api.putLabConfig({ label_studio_url: cfg.url.trim(), default_project_id: cfg.pid || 0, task_source: taskSource });
        Object.assign(cfg, { url: c.label_studio_url, pid: c.default_project_id, savedSrc: c.task_source, src: c.task_source });
        cfg.effective = (await api.labTasks({ limit: 1, offset: 0 })).source;
        toast('已保存', 'ok');
      } catch (e) {
        toastError('保存配置', e);
      } finally {
        cfg.saving = false;
      }
    }
    const saveLs = () => putConfig(null);               // null = 不改任务来源
    const saveSource = () => putConfig(cfg.src);

    async function runProbe() {
      probe.busy = true;
      probe.res = null;
      const n = Math.min(200, Math.max(1, probe.n));
      const c = Math.min(10, Math.max(1, probe.c));
      const xs = [];
      let errors = 0;
      let left = n;
      async function worker() {
        while (left > 0) {
          left -= 1;
          const t0 = performance.now();
          try { await api.raw(probe.ep); xs.push(performance.now() - t0); } catch { errors += 1; }
        }
      }
      await Promise.all(Array.from({ length: c }, worker));
      xs.sort((a, b) => a - b);
      if (xs.length) {
        const q = (p) => xs[Math.min(xs.length - 1, Math.floor(p * xs.length))].toFixed(1);
        const lo = xs[0];
        const hi = xs[xs.length - 1];
        const bins = Array(20).fill(0);
        xs.forEach((x) => { bins[Math.min(19, Math.floor((x - lo) / (hi - lo + 1e-9) * 20))] += 1; });
        probe.res = {
          stat: { 最小: lo.toFixed(1), 平均: (xs.reduce((a, b) => a + b, 0) / xs.length).toFixed(1), 最大: hi.toFixed(1), P50: q(0.5), P95: q(0.95), 错误: errors },
          bins, maxBin: Math.max(...bins), lo: lo.toFixed(1), hi: hi.toFixed(1),
        };
      } else {
        toast(`全部 ${n} 次请求都失败了`, 'bad');
      }
      probe.busy = false;
    }

    onMounted(async () => {
      metrics.start();
      healthPoll.start();
      overview.start();
      loadConfig();
      try { monitorConfig.value = (await api.monitorConfig()).config || {}; } catch (e) { toastError('读取监控配置', e); }
    });
    onBeforeUnmount(() => { metrics.stop(); healthPoll.stop(); overview.stop(); });

    return {
      CHART, PROBE_ENDPOINTS, history, quant, hidden, models, color, yTicks, y, pathOf, toggleModel, counters,
      health, monitorNumbers, reconnecting, monitorConfig, queues, queueKeys, probe, runProbe, cfg, effTag, saveLs, saveSource, fmtWall,
    };
  },
};

export const systemCrumbs = () => [{ t: '系统运维' }];
