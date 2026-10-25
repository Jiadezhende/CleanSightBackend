// 回放工作台的离线分析：分割段 / 逐帧概率 / 离线违规三份结果，作业提交与跑完自动刷新，以及两条泳道。
//   const off = useOffline({ ids, invalidate });
//   off.load()（换 run 后、作业跑完后调）  off.lanes（交给 Timeline）  off.submit()
import { adaptSegment, adaptViolation } from '../api/adapters.js';
import { stageOf } from '../api/catalog.js';
import { api } from '../api/endpoints.js';
import { jobOf, refreshJobs } from '../api/jobs.js';
import { fmtDur, fmtMedia } from '../core/format.js';
import { labelColor } from '../core/levels.js';
import { toast, toastError } from '../core/ui.js';

const { ref, shallowRef, computed, watch } = Vue;

const FLASH_MS = 1600;
const ACTIVE = ['queued', 'running'];

export function useOffline({ ids, invalidate }) {
  const segs = ref([]);
  const viols = ref([]);
  const checked = ref(false);          // 离线规则核对过没有（没核对 ≠ 核对过没违规）
  const probs = shallowRef(null);      // { labels, mediaMs, p }，没有概率产物为 null
  const labels = ref([]);              // 类别顺序：概率的 labels，否则按分割段出现顺序
  const loaded = ref(false);
  const hl = ref(null);                // 高亮的类别
  const probsOpen = ref(false);
  const submitting = ref(false);
  let flashUntil = 0;
  let loadSeq = 0;

  const stage = computed(() => stageOf(ids.step));
  const job = computed(() => (ids.run ? jobOf(ids.task, ids.step, ids.run) : null));
  const busy = computed(() => !!job.value && ACTIVE.includes(job.value.status));
  const hasResult = computed(() => segs.value.length > 0);
  const labelStat = computed(() => {
    const o = Object.fromEntries(labels.value.map((k) => [k, { n: 0, ms: 0 }]));
    for (const s of segs.value) {
      if (!o[s.label]) o[s.label] = { n: 0, ms: 0 };
      o[s.label].n += 1;
      o[s.label].ms += s.endMs - s.startMs;
    }
    return o;
  });

  /** 泳道没有内容时说明为什么。 */
  const emptyReason = computed(() => {
    if (!stage.value.has_offline_model) return `${stage.value.alias} 没有离线模型`;
    const j = job.value;
    if (j && j.status === 'queued') return '离线推理排队中…';
    if (j && j.status === 'running') return '离线推理运行中…';
    if (j && j.status === 'failed') return `离线推理失败：${j.message}`;
    if (!loaded.value) return '加载中…';
    return hasResult.value ? '' : '未跑离线推理';
  });

  function reset() {
    segs.value = [];
    viols.value = [];
    checked.value = false;
    probs.value = null;
    labels.value = [];
    hl.value = null;
  }

  async function load() {
    const seq = ++loadSeq;
    reset();
    loaded.value = false;
    if (!stage.value.has_offline_model || !ids.run) { loaded.value = true; invalidate(); return; }
    const who = { task_id: ids.task, step_id: ids.step, run_id: ids.run, track: 'raw' };
    const [tmp, lp, al] = await Promise.all([
      api.temporal(who).catch((e) => { toastError('读取分割结果', e); return null; }),
      api.labelProbs(who).catch(() => null),
      api.offlineAlarms(who).catch(() => null),
    ]);
    if (seq !== loadSeq) return;
    segs.value = tmp ? tmp.items.map(adaptSegment) : [];
    if (lp && lp.labels.length) {
      probs.value = { labels: lp.labels, mediaMs: lp.media_ms, p: lp.probs };
      labels.value = lp.labels;
    } else {
      labels.value = [...new Set(segs.value.map((s) => s.label))];
    }
    checked.value = !!(al && al.checked);
    viols.value = al ? al.items.map(adaptViolation) : [];
    loaded.value = true;
    invalidate();
  }

  async function submit() {
    submitting.value = true;
    try {
      await api.submitOfflineJob({ task_id: ids.task, step_id: ids.step, run_id: ids.run });
      toast(`已提交离线推理：任务 ${ids.task} · step ${ids.step}`, 'info');
      await refreshJobs();
    } catch (e) {
      toastError('提交离线推理', e);
    } finally {
      submitting.value = false;
    }
  }

  // 本 run 的作业跑完：自动重取三份结果，泳道闪一下
  watch(() => (job.value ? job.value.status : null), (now, before) => {
    if (now === 'completed' && before && before !== 'completed') {
      load();
      flashUntil = performance.now() + FLASH_MS;
    }
  });
  watch([hl, probsOpen], invalidate);

  const color = (k) => labelColor(labels.value, k);
  const alpha = (k) => (hl.value && hl.value !== k ? 0.16 : 0.92);
  const segAt = (m) => segs.value.find((s) => m >= s.startMs && m <= s.endMs) || null;

  const segLane = {
    key: 'seg',
    name: '分割段',
    height: 26,
    draw(c, tl, l) {
      for (const s of segs.value) {
        if (s.endMs < tl.view.v0 || s.startMs > tl.view.v1) continue;
        const x0 = tl.m2x(s.startMs);
        const x1 = tl.m2x(s.endMs);
        c.globalAlpha = alpha(s.label);
        c.fillStyle = color(s.label);
        c.fillRect(x0, l.y + 4, Math.max(1, x1 - x0 - 1), l.h - 8);
        if (x1 - x0 > 70) {
          c.fillStyle = '#fff'; c.font = '11px system-ui'; c.textBaseline = 'middle';
          c.fillText(s.label, x0 + 5, l.y + l.h / 2 + 1);
        }
        c.globalAlpha = 1;
      }
      const left = flashUntil - performance.now();
      if (left > 0) { c.fillStyle = `rgba(37,99,235,${(left / FLASH_MS) * 0.3})`; c.fillRect(0, l.y, tl.W, l.h); tl.invalidate(); }
    },
    empty: () => (hasResult.value ? '' : emptyReason.value),
    tip(_x, m) {
      const s = segAt(m);
      return s ? [`${s.label}  ${fmtMedia(s.startMs)}–${fmtMedia(s.endMs)} · ${fmtDur(s.endMs - s.startMs)} · conf ${s.conf.toFixed(2)}`] : [];
    },
  };

  const probLane = {
    key: 'prob',
    name: () => (probsOpen.value ? '概率 ▾' : '概率 ▸'),
    height: () => (probsOpen.value ? 76 : 18),
    onLabelClick: () => { probsOpen.value = !probsOpen.value; },
    draw(c, tl, l) {
      const P = probs.value;
      if (!probsOpen.value || !P) return;
      const t = P.mediaMs;
      let i0 = 0;
      while (i0 < t.length && t[i0] < tl.view.v0) i0 += 1;
      i0 = Math.max(0, i0 - 1);
      let i1 = i0;
      while (i1 < t.length - 1 && t[i1] <= tl.view.v1) i1 += 1;
      const stepI = Math.max(1, Math.floor((i1 - i0) / (tl.inner() * 1.5)));
      const order = P.labels.map((_, ci) => ci).sort((a, b) => (P.labels[a] === hl.value) - (P.labels[b] === hl.value));
      for (const ci of order) {
        const k = P.labels[ci];
        c.globalAlpha = hl.value && hl.value !== k ? 0.13 : 0.9;
        c.strokeStyle = color(k);
        c.lineWidth = hl.value === k ? 2.2 : 1.2;
        c.beginPath();
        for (let i = i0; i <= i1; i += stepI) {
          const x = tl.m2x(t[i]);
          const y = l.y + l.h - 4 - P.p[ci][i] * (l.h - 8);
          if (i === i0) c.moveTo(x, y); else c.lineTo(x, y);
        }
        c.stroke();
      }
      c.globalAlpha = 1;
    },
    empty: () => {
      if (!probsOpen.value) return '';
      if (probs.value) return '';
      return hasResult.value ? '该模型不产逐帧概率' : emptyReason.value;
    },
    tip(_x, m) {
      const P = probs.value;
      if (!probsOpen.value || !P || !P.mediaMs.length) return [];
      let i = 0;
      while (i < P.mediaMs.length - 1 && P.mediaMs[i + 1] <= m) i += 1;
      return P.labels.map((k, ci) => [k, P.p[ci][i]]).sort((a, b) => b[1] - a[1]).slice(0, 3)
        .map(([k, v]) => `${k}  ${v.toFixed(2)}`);
    },
  };

  return {
    segs, viols, checked, probs, labels, loaded, hl, probsOpen, submitting, stage, job, busy, hasResult, labelStat,
    emptyReason, color, load, submit, lanes: [segLane, probLane],
  };
}
