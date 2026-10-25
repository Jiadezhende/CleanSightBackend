// 回放工作台的送标：片段增删改、按断点拆分、提交到 Label Studio、按 run 暂存与离开保护，以及时间轴上的「送标片段」泳道。
//   const label = useLabel({ ids, gaps, canMark, durationMs, invalidate });
//   label.load()（换 run 后调）  label.lane（交给 Timeline）  label.addClip(s, e)  label.submit()
import { api } from '../api/endpoints.js';
import { fmtDur, fmtMedia } from '../core/format.js';
import { roundRect } from '../components/timeline.js';
import { setLeaveGuard } from '../core/router.js';
import { toast, toastError } from '../core/ui.js';

const { ref, reactive, computed, watch } = Vue;

const MIN_CLIP_MS = 1000;      // 拖选不足这个长度视为误触
const EVENT_PAD_MS = 5000;     // 「以事件生成片段」前后各取多少

const STATUS_COLOR = { ok: '#16a34a', fail: '#dc2626', sending: '#ca8a04', new: '#2563eb' };

export function useLabel({ ids, gaps, canMark, durationMs, invalidate }) {
  const clips = ref([]);
  const draft = ref(null);                 // 拖选中的区间 { s, e }
  const submitting = ref(false);
  const waited = ref(0);
  const project = ref(0);
  const ls = reactive({ url: '', configured: null, reachable: null, error: '' });
  const limits = reactive({ max_clips: 0, max_clip_ms: 0, max_total_ms: 0 });

  const storeKey = () => `cs-console-clips:${ids.task}/${ids.step}/${ids.run}`;
  const gapsIn = (s, e) => gaps.value.filter((g) => g.mediaMs > s && g.mediaMs < e);
  const clipTotal = computed(() => clips.value.reduce((a, c) => a + c.e - c.s, 0));
  const todoCount = computed(() => clips.value.filter((c) => c.status !== 'ok').length);
  const doneCount = computed(() => clips.value.filter((c) => c.status === 'ok').length);
  const clipErr = computed(() => (limits.max_total_ms && clipTotal.value > limits.max_total_ms
    ? `总时长超过 ${fmtDur(limits.max_total_ms)}` : ''));

  function save() {
    sessionStorage.setItem(storeKey(), JSON.stringify(clips.value.map((c) => (c.status === 'sending' ? { ...c, status: 'new' } : c))));
  }
  watch(clips, () => { invalidate(); if (ids.run) save(); }, { deep: true });
  watch(draft, invalidate);

  /** 换 run 后调：恢复本 run 暂存的片段，读 LS 配置与上限。 */
  async function load() {
    try { clips.value = JSON.parse(sessionStorage.getItem(storeKey()) || '[]'); } catch { clips.value = []; }
    if (clips.value.length) toast(`已恢复本 run 暂存的 ${clips.value.length} 段送标片段`, 'info');
    try {
      const cfg = await api.labConfig();
      Object.assign(limits, cfg.limits);
      ls.url = cfg.label_studio_url;
      if (!project.value) project.value = cfg.default_project_id;
    } catch (e) {
      toastError('读取送标配置', e);
    }
  }

  async function checkLs() {
    try {
      const h = await api.labHealth();
      Object.assign(ls, { url: h.label_studio_url || ls.url, configured: h.configured, reachable: h.reachable, error: h.error || '' });
    } catch (e) {
      Object.assign(ls, { configured: null, reachable: false, error: e.message });
    }
    return ls.configured && ls.reachable;
  }

  function addClip(s, e) {
    const dur = durationMs();
    s = Math.round(Math.min(dur, Math.max(0, s)));
    e = Math.round(Math.min(dur, Math.max(0, e)));
    if (e - s < MIN_CLIP_MS) return false;
    if (limits.max_clip_ms && e - s > limits.max_clip_ms) {
      e = s + limits.max_clip_ms;
      toast(`单段上限 ${fmtDur(limits.max_clip_ms)}，已截断`, 'warn');
    }
    const i = clips.value.findIndex((c) => c.s < e && s < c.e);
    if (i >= 0) { toast(`与片段 #${i + 1} 重叠，未加入`, 'warn'); return false; }
    if (limits.max_clips && clips.value.length >= limits.max_clips) { toast(`最多 ${limits.max_clips} 段`, 'warn'); return false; }
    clips.value = [...clips.value, { id: `${Date.now()}-${Math.random()}`, s, e, status: 'new' }].sort((a, b) => a.s - b.s);
    return true;
  }

  /** 以事件时刻为中心取前后 5 秒；碰到断流就在断点处截短。 */
  function clipFromEvent(ev) {
    const m = ev.mediaMs;
    let s = Math.max(0, m - EVENT_PAD_MS);
    let e = Math.min(durationMs(), m + EVENT_PAD_MS);
    let cut = false;
    for (const g of gapsIn(s, e)) {
      cut = true;
      if (g.mediaMs <= m) s = g.mediaMs; else e = g.mediaMs;
    }
    if (addClip(s, e)) toast(`已加入送标片段 ${fmtMedia(s)}–${fmtMedia(e)}${cut ? '（已在断点处截短）' : ''}`, 'ok');
  }

  function removeClip(i) { clips.value = clips.value.filter((_, k) => k !== i); }

  function splitClip(i) {
    const c = clips.value[i];
    const pts = [c.s, ...gapsIn(c.s, c.e).map((g) => g.mediaMs), c.e];
    const parts = [];
    for (let k = 0; k < pts.length - 1; k++) {
      if (pts[k + 1] - pts[k] >= MIN_CLIP_MS) parts.push({ id: `${Date.now()}-${k}`, s: pts[k], e: pts[k + 1], status: 'new' });
    }
    clips.value = [...clips.value.slice(0, i), ...parts, ...clips.value.slice(i + 1)];
    toast(`已按断点拆成 ${parts.length} 段`, 'ok');
  }

  /** 只提交未成功的段；已成功的锁定，避免在 LS 里重复建任务。 */
  async function submit() {
    if (!(await checkLs())) {
      toast(`Label Studio ${ls.configured === false ? '未配置' : '不可达'}：${ls.error}`, 'bad', null, 6000);
      return;
    }
    const todo = clips.value.filter((c) => c.status !== 'ok');
    if (!todo.length) return;
    submitting.value = true;
    waited.value = 0;
    const timer = setInterval(() => { waited.value += 1; }, 1000);
    todo.forEach((c) => { c.status = 'sending'; });
    try {
      const body = {
        task_id: ids.task, step_id: ids.step, run_id: ids.run,
        clips: todo.map((c) => ({ start_media_ms: c.s, end_media_ms: c.e })),
      };
      if (project.value > 0) body.project_id = project.value;
      const res = await api.labSubmit(body);
      const byRange = Object.fromEntries(res.clips.map((r) => [`${r.start_media_ms}-${r.end_media_ms}`, r]));
      for (const c of todo) {
        const r = byRange[`${c.s}-${c.e}`];
        if (r && r.success) Object.assign(c, { status: 'ok', lsId: r.label_studio_task_id, projectId: res.project_id, errCode: null, errMsg: '' });
        else Object.assign(c, { status: 'fail', errCode: r ? r.error_code : null, errMsg: r ? (r.error || r.error_code) : '后端未返回该段结果' });
      }
      if (res.failure_count === 0) toast(`送标完成：${res.success_count} 段全部成功`, 'ok');
      else toast(`送标部分成功：${res.success_count}/${res.total}，失败的段可修正后单独重提`, 'warn', null, 6000);
    } catch (e) {
      todo.forEach((c) => { if (c.status === 'sending') c.status = 'new'; });
      toastError('送标', e);
    } finally {
      clearInterval(timer);
      submitting.value = false;
      clips.value = [...clips.value];
    }
  }

  const lsTaskUrl = (c) => (ls.url ? `${ls.url}/projects/${c.projectId}/data?task=${c.lsId}` : null);

  // ── 离开保护：有未提交片段时，离开本 run 前确认（片段已暂存，回来能恢复） ──
  setLeaveGuard((next) => {
    if (!todoCount.value) return null;
    if (next && next.name === 'run' && next.parts.join('/') === `${ids.task}/${ids.step}/${ids.run}`) return null;
    return `有 ${todoCount.value} 段送标片段尚未提交。它们已暂存在本标签页，回到这个 run 时会恢复。`;
  });
  const dispose = () => setLeaveGuard(null);

  // ── 时间轴「送标片段」泳道 ──
  function edgeHit(x, tl) {
    for (let i = 0; i < clips.value.length; i++) {
      const c = clips.value[i];
      if (c.status === 'ok' || c.status === 'sending') continue;
      if (Math.abs(tl.m2x(c.s) - x) < 6) return { i, edge: 's' };
      if (Math.abs(tl.m2x(c.e) - x) < 6) return { i, edge: 'e' };
    }
    return null;
  }
  const clipIndexAt = (x, tl) => clips.value.findIndex((c) => x >= tl.m2x(c.s) && x <= tl.m2x(c.e));

  const lane = {
    key: 'clip',
    name: '送标片段',
    height: 30,
    draw(c, tl, l) {
      clips.value.forEach((cp, i) => {
        const x0 = tl.m2x(cp.s);
        const x1 = tl.m2x(cp.e);
        const w = Math.max(2, x1 - x0);
        const col = STATUS_COLOR[cp.status] || STATUS_COLOR.new;
        c.fillStyle = `${col}22`; c.strokeStyle = col; c.lineWidth = 1.5;
        roundRect(c, x0, l.y + 5, w, l.h - 10, 4); c.fill(); c.stroke();
        if (cp.status !== 'ok') { c.fillStyle = col; c.fillRect(x0, l.y + 5, 3, l.h - 10); c.fillRect(x1 - 3, l.y + 5, 3, l.h - 10); }
        if (w > 20) {
          c.fillStyle = col; c.font = '600 11px system-ui'; c.textBaseline = 'middle';
          c.fillText(`#${i + 1}${cp.status === 'ok' ? ' ✓' : ''}`, x0 + 6, l.y + l.h / 2 + 1);
        }
      });
      if (draft.value) {
        const x0 = tl.m2x(draft.value.s);
        const w = Math.max(2, tl.m2x(draft.value.e) - x0);
        c.strokeStyle = '#f59e0b'; c.setLineDash([4, 3]); c.lineWidth = 1.5; c.fillStyle = 'rgba(245,158,11,.14)';
        c.fillRect(x0, l.y + 5, w, l.h - 10); c.strokeRect(x0, l.y + 5, w, l.h - 10); c.setLineDash([]);
      }
    },
    empty: () => (clips.value.length || draft.value ? '' : (canMark() ? '在此拖选即生成送标片段' : 'processed 视图下不能打点')),
    cursor: (x, tl) => (canMark() ? (edgeHit(x, tl) ? 'ew-resize' : 'crosshair') : 'default'),
    onPointerDown(x, tl) {
      if (!canMark()) return null;
      const h = edgeHit(x, tl);
      if (h) return { type: 'resize', ...h };
      if (clipIndexAt(x, tl) >= 0) return null;
      return { type: 'create', m0: tl.clampMs(tl.x2m(x)) };
    },
    onDrag(d, x, tl) {
      const m = tl.clampMs(tl.x2m(x));
      if (d.type === 'create') { draft.value = { s: Math.min(d.m0, m), e: Math.max(d.m0, m) }; return; }
      const list = clips.value;
      const c = list[d.i];
      const prevE = d.i > 0 ? list[d.i - 1].e : 0;
      const nextS = d.i < list.length - 1 ? list[d.i + 1].s : durationMs();
      const r = Math.round(m);
      if (d.edge === 's') c.s = Math.min(Math.max(r, prevE), c.e - MIN_CLIP_MS);
      else c.e = Math.max(Math.min(r, nextS), c.s + MIN_CLIP_MS);
      if (c.status === 'fail') c.status = 'new';
      invalidate();
    },
    onDragEnd(d) {
      if (d.type === 'create' && draft.value) {
        const { s, e } = draft.value;
        draft.value = null;
        addClip(s, e);
      } else {
        clips.value = [...clips.value];
      }
    },
    hit(x, tl) {
      const i = clipIndexAt(x, tl);
      return i >= 0 ? { kind: 'clip', clip: clips.value[i] } : null;
    },
    tip(x, _m, tl) {
      const i = clipIndexAt(x, tl);
      if (i < 0) return [];
      const c = clips.value[i];
      return [`片段 #${i + 1} ${fmtMedia(c.s)}–${fmtMedia(c.e)} · ${fmtDur(c.e - c.s)}`];
    },
  };

  return {
    clips, draft, submitting, waited, project, ls, limits, clipTotal, todoCount, doneCount, clipErr,
    load, checkLs, addClip, clipFromEvent, removeClip, splitClip, submit, gapsIn, lsTaskUrl, lane, dispose,
  };
}
