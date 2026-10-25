// 回放工作台 #/run/{task}/{step}/{run|latest}?t=&mode=raw|processed|sbs&ev=&panel=review|label
// 进入时把 run 落成具体 run_id 写回 URL，之后本页所有请求都带这个 run_id。
import { adaptGap, adaptTimelineAlarm } from '../api/adapters.js';
import { stageOf } from '../api/catalog.js';
import { api } from '../api/endpoints.js';
import { parsePlaylist } from '../api/playlist.js';
import { DualPlayer } from '../components/dual-player.js';
import { Timeline } from '../components/timeline.js';
import { fmtDur, fmtMedia, fmtWall, fmtWallMs } from '../core/format.js';
import { lvCls, lvColor } from '../core/levels.js';
import { replacePath, replaceQuery, route } from '../core/router.js';
import { toast } from '../core/ui.js';
import { useLabel } from './run-label.js';

const { ref, shallowRef, reactive, computed, watch, onMounted, onBeforeUnmount, nextTick } = Vue;

function eventLane(getEvents, getSelected) {
  const timed = () => getEvents().filter((e) => e.mediaMs != null);
  return {
    key: 'event',
    name: '事件',
    height: 28,
    draw(c, tl, l) {
      const cy = l.y + l.h / 2;
      for (const ev of timed()) {
        const x = tl.m2x(ev.mediaMs);
        if (ev.id === getSelected()) {
          c.fillStyle = 'rgba(37,99,235,.18)';
          c.beginPath(); c.arc(x, cy, 11, 0, 7); c.fill();
        }
        c.fillStyle = lvColor(ev.level);
        c.beginPath();
        if (ev.source === 'alarm') { c.moveTo(x - 6, cy - 6); c.lineTo(x + 6, cy - 6); c.lineTo(x, cy + 6); }
        else { c.moveTo(x, cy - 7); c.lineTo(x + 6, cy); c.lineTo(x, cy + 7); c.lineTo(x - 6, cy); }
        c.closePath(); c.fill();
      }
    },
    hit(x, tl) {
      let best = null;
      let bd = 8;
      for (const ev of timed()) {
        const d = Math.abs(tl.m2x(ev.mediaMs) - x);
        if (d < bd) { bd = d; best = ev; }
      }
      return best;
    },
    tip(x, _m, tl) {
      const ev = this.hit(x, tl);
      return ev ? [`${ev.source === 'alarm' ? '平台告警' : '离线违规'} · ${ev.level ?? '—'} · ${ev.kind ?? '—'}`, ev.message ?? ''] : [];
    },
    empty: () => (timed().length ? '' : '本 run 没有带时刻的事件'),
  };
}

export const RunPage = {
  template: `
<div class="wb">
  <div class="run-head">
    <div class="rh-title">
      <span class="alias">{{ alias }}</span>
      任务 <b class="mono">{{ ids.task }}</b> · step {{ ids.step }}
      <span v-if="ids.run" class="mono sub">run {{ fmtWall(ids.run, true) }}</span>
    </div>
    <div class="rh-meta mono" v-if="meta">
      时长 {{ fmtMedia(meta.durationMs, false) }} · 墙钟 {{ fmtWall(meta.startMs) }}–{{ fmtWall(meta.endMs) }}
      <span v-if="gaps.length" class="gapinfo"> · 断流 {{ gaps.length }} 处 共 {{ fmtDur(meta.gapTotalMs) }}</span>
    </div>
    <div class="sseg" v-if="meta">
      <button :class="{on: mode==='raw'}" @click="setMode('raw')">raw</button>
      <button :class="{on: mode==='processed'}" :disabled="!hasProc" @click="setMode('processed')">processed</button>
      <button :class="{on: mode==='sbs'}" :disabled="!hasProc" @click="setMode('sbs')">并排对照</button>
    </div>
    <div style="position:relative" v-if="meta">
      <button class="sbtn" @click.stop="dlOpen = !dlOpen">⬇ 下载 ▾</button>
      <div class="dl-menu" v-if="dlOpen" @click.stop>
        <a :href="downloadUrl('raw')" download @click="onDownload">raw 整段 mp4</a>
        <a v-if="hasProc" :href="downloadUrl('processed')" download @click="onDownload">processed 整段 mp4</a>
      </div>
    </div>
  </div>

  <div v-if="error" class="page"><div class="card card-b">{{ error }}</div></div>
  <div class="run-body" v-show="!error">
    <div class="stage">
      <div class="players" :class="mode">
        <div class="pv" v-show="mode!=='processed'">
          <video ref="rawV" muted playsinline></video>
          <span class="pv-tag main">RAW</span>
        </div>
        <div class="pv" v-show="mode!=='raw'">
          <video ref="procV" muted playsinline></video>
          <span class="pv-tag" :class="{main: mode==='processed'}">PROCESSED</span>
          <div class="noframe" v-if="mode==='sbs' && !procHasFrame"><b>此刻无标注画面</b><span>{{ noFrameReason }}</span></div>
        </div>
        <div class="gap-flash" v-if="gapFlash">跨越断流 {{ fmtDur(gapFlash.gapMs) }}</div>
      </div>

      <div class="transport">
        <button class="play" @click="toggle" :title="playing ? '暂停' : '播放'">{{ playing ? '❚❚' : '▶' }}</button>
        <span class="mono"><span class="tnow">{{ fmtMedia(pos) }}</span> <span class="tdur">/ {{ fmtMedia(meta ? meta.durationMs : 0) }}</span></span>
        <span class="twall mono" v-if="wallNow != null" title="当前帧的墙钟时刻">{{ fmtWallMs(wallNow) }}</span>
        <select class="run-sel" v-model.number="rate"><option :value="1">1×</option><option :value="2">2×</option><option :value="4">4×</option></select>
        <span v-if="drift != null" class="sub mono" title="并排时 processed 相对 raw 的对时偏差">偏差 {{ drift }} ms</span>
        <span style="margin-left:auto"></span>
        <button class="sbtn sm" @click="fit">适应全程</button>
      </div>

      <div class="tl-wrap" ref="tlWrap">
        <canvas ref="tlCv"></canvas>
        <div class="tl-tip" v-if="hover" :style="{left: hover.x + 'px', top: hover.y + 'px'}">
          <div v-for="(l, i) in hover.lines" :key="i" :class="i === 0 ? 't1' : 't2'">{{ l }}</div>
        </div>
      </div>
      <div class="keys">空格 播放/暂停 · ←/→ 前后 1 秒 · 滚轮缩放、拖动平移 · 在「送标片段」上拖选即生成片段，拖边缘微调</div>
    </div>

    <aside class="dock">
      <div class="dock-tabs">
        <button :class="{on: panel==='review'}" @click="setPanel('review')">事件 <span class="cnt" :class="{hot: events.length}">{{ events.length }}</span></button>
        <button :class="{on: panel==='label'}" @click="setPanel('label')">送标 <span class="cnt">{{ label.clips.value.length }}</span></button>
      </div>
      <template v-if="panel==='review'">
      <div class="dock-body">
        <div v-for="ev in events" :key="ev.id" class="ev" :class="{on: selEv === ev.id}" @click="pickEvent(ev)">
          <span class="ico" :class="ev.source">{{ ev.source === 'alarm' ? '▼' : '◆' }}</span>
          <span class="et mono" :class="{whole: ev.mediaMs == null}">{{ fmtMedia(ev.mediaMs) }}</span>
          <span class="ek"><span class="lv" :class="lvCls(ev.level)">{{ ev.level || '—' }}</span><span class="kn">{{ ev.kind || '—' }}</span>
            <span class="src">{{ ev.source === 'alarm' ? '平台告警' : '离线违规' }}</span></span>
          <span class="em">{{ ev.message }}</span>
        </div>
        <div v-if="!events.length" class="empty">{{ loading ? '加载中…' : '本 run 没有事件' }}</div>
      </div>
      <div class="dock-foot" v-if="selEvObj && selEvObj.mediaMs != null">
        <button class="sbtn primary" style="width:100%;justify-content:center" :disabled="!canMark()" @click="label.clipFromEvent(selEvObj)">生成前后 5 秒的送标片段</button>
        <div class="note" v-if="!canMark()">processed 视图下不能打点：送标只裁 raw，切回 raw 或并排对照。</div>
      </div>
      </template>

      <template v-if="panel==='label'">
        <div class="dock-body">
          <div class="ls-row">
            <i class="dot" :class="lsDot"></i>{{ lsText }}
            <span style="margin-left:auto">project</span>
            <input class="sinp mono" type="number" min="0" v-model.number="label.project.value" title="默认值取自 LS 配置">
          </div>
          <div class="dk-sec">
            <h5>送标片段 <span class="mono" style="margin-left:auto">{{ label.clips.value.length }}/{{ label.limits.max_clips }} · {{ fmtDur(label.clipTotal.value) }}/{{ fmtDur(label.limits.max_total_ms) }}</span></h5>
            <table class="stb" v-if="label.clips.value.length">
              <thead><tr><th>#</th><th>区间</th><th>时长</th><th>状态</th><th></th></tr></thead>
              <tbody>
                <template v-for="(c, i) in label.clips.value" :key="c.id">
                  <tr @click="seek(c.s)" style="cursor:pointer">
                    <td class="mono">{{ i + 1 }}</td>
                    <td class="mono">{{ fmtMedia(c.s) }}–{{ fmtMedia(c.e) }}</td>
                    <td class="mono">{{ fmtDur(c.e - c.s) }}</td>
                    <td>
                      <span v-if="c.status==='ok'" class="st-ok">✓ <a v-if="label.lsTaskUrl(c)" :href="label.lsTaskUrl(c)" target="_blank" @click.stop>LS#{{ c.lsId }}</a><span v-else>LS#{{ c.lsId }}</span></span>
                      <span v-else-if="c.status==='fail'" class="st-bad">✗ 失败</span>
                      <span v-else-if="c.status==='sending'" class="st-send">上传中…</span>
                      <span v-else class="st-new">待提交</span>
                    </td>
                    <td style="text-align:right">
                      <span v-if="c.status==='ok'" class="sub">已锁定</span>
                      <a v-else-if="c.status!=='sending'" class="sub" @click.stop="label.removeClip(i)" title="删除">✕</a>
                    </td>
                  </tr>
                  <tr v-if="c.status!=='ok' && c.status!=='sending' && (c.status==='fail' || label.gapsIn(c.s, c.e).length)" class="sub-row">
                    <td></td>
                    <td colspan="4">
                      <span :class="c.status==='fail' ? 'st-bad' : 'st-send'">{{ c.status==='fail' ? c.errMsg : '跨越断流，提交会失败' }}</span>
                      <a v-if="label.gapsIn(c.s, c.e).length" style="margin-left:10px" @click.stop="label.splitClip(i)">按断点拆分</a>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
            <div v-else class="box sub">还没有片段。在时间轴「送标片段」上拖选，或在「事件」里选一条后生成。</div>
          </div>
        </div>
        <div class="dock-foot">
          <div class="errline" v-if="label.clipErr.value">{{ label.clipErr.value }}</div>
          <button class="sbtn primary" style="width:100%;justify-content:center;height:34px"
            :disabled="label.submitting.value || !label.todoCount.value || !!label.clipErr.value" @click="label.submit()">
            {{ label.submitting.value ? '裁剪并上传中… 已等待 ' + label.waited.value + 's'
              : (label.doneCount.value ? '提交未成功的 ' + label.todoCount.value + ' 段' : '提交送标（' + label.todoCount.value + ' 段）') }}
          </button>
        </div>
      </template>
    </aside>
  </div>
</div>`,
  setup() {
    const ids = reactive({ task: 0, step: 0, run: 0 });
    const meta = ref(null);            // { durationMs, startMs, endMs, gapTotalMs }
    const gaps = ref([]);
    const events = ref([]);
    const loading = ref(true);
    const error = ref('');
    const mode = ref('raw');
    const hasProc = ref(false);
    const playing = ref(false);
    const pos = ref(0);
    const rate = ref(1);
    const drift = ref(null);
    const procHasFrame = ref(true);
    const selEv = ref(null);
    const panel = ref(route.query.panel === 'label' ? 'label' : 'review');
    const gapFlash = ref(null);
    const hover = ref(null);
    const dlOpen = ref(false);
    const rawV = ref(null);
    const procV = ref(null);
    const tlCv = ref(null);
    const tlWrap = ref(null);
    let player = null;
    let timeline = null;
    const maps = shallowRef({ raw: null, processed: null });   // 两轨的媒体轴 ↔ 墙钟对照表
    let raf = 0;
    let loadSeq = 0;

    const alias = computed(() => stageOf(ids.step).alias);
    const selEvObj = computed(() => events.value.find((e) => e.id === selEv.value) || null);
    const canMark = () => mode.value !== 'processed';
    const label = useLabel({
      ids, gaps, canMark,
      durationMs: () => (meta.value ? meta.value.durationMs : 0),
      invalidate: () => { if (timeline) timeline.invalidate(); },
    });
    const lsDot = computed(() => (label.ls.reachable ? '' : (label.ls.configured === null && !label.ls.error ? 'off' : 'bad')));
    const lsText = computed(() => {
      const s = label.ls;
      if (s.reachable) return 'LS 可达';
      if (s.configured === false) return 'LS 未配置';
      return s.error ? `LS 不可达：${s.error}` : 'LS 检查中…';
    });
    const wallNow = computed(() => (maps.value.raw ? maps.value.raw.wallAt(pos.value) : null));
    const noFrameReason = computed(() => {
      const { raw, processed: p } = maps.value;
      const w = wallNow.value;
      if (!p || !p.segs.length || w == null) return '';
      if (w < p.segs[0].wallMs) return `processed 首段晚于 raw ${fmtDur(p.segs[0].wallMs - raw.segs[0].wallMs)}（推理预热）`;
      return 'processed 此刻没有画面（断流或推理中断）';
    });

    async function load() {
      if (route.name !== 'run') return;
      const [task, step, runParam] = route.parts;
      if (!task || !step) { error.value = '从任务总览点一个 step 打开回放'; loading.value = false; return; }
      const seq = ++loadSeq;
      loading.value = true;
      error.value = '';
      meta.value = null;
      events.value = [];
      gaps.value = [];
      player.pause();
      try {
        const runArg = runParam && runParam !== 'latest' ? Number(runParam) : null;
        const tl = await api.timeline(task, step, runArg, 'raw');
        if (seq !== loadSeq) return;
        if (tl.run_id == null || !tl.media_duration_ms) throw new Error('该 step 没有可回放的 raw 录像');
        if (String(tl.run_id) !== runParam) replacePath(`/run/${task}/${step}/${tl.run_id}`);
        Object.assign(ids, { task: Number(task), step: Number(step), run: tl.run_id });
        label.load();

        const rawText = await api.playlistText(task, step, tl.run_id, 'raw');
        const procText = await api.playlistText(task, step, tl.run_id, 'processed').catch((e) => {
          if (e.status === 404 || e.status === 503) return null;
          throw e;
        });
        if (seq !== loadSeq) return;
        const rawMap = parsePlaylist(rawText);
        const procMap = procText ? parsePlaylist(procText) : null;
        hasProc.value = !!(procMap && procMap.hasWall && rawMap.hasWall);
        maps.value = { raw: rawMap, processed: hasProc.value ? procMap : null };

        meta.value = { durationMs: tl.media_duration_ms, startMs: tl.start_ms, endMs: tl.end_ms, gapTotalMs: tl.gap_total_ms };
        gaps.value = tl.gaps.map(adaptGap);
        events.value = tl.events.map(adaptTimelineAlarm);
        selEv.value = route.query.ev || null;

        mode.value = hasProc.value ? (['raw', 'processed', 'sbs'].includes(route.query.mode) ? route.query.mode : 'sbs') : 'raw';
        const t = Math.min(Number(route.query.t || 0), meta.value.durationMs);
        player.load({
          rawUrl: api.playlistUrl(task, step, tl.run_id, 'raw'),
          procUrl: hasProc.value ? api.playlistUrl(task, step, tl.run_id, 'processed') : null,
          rawMap: maps.value.raw,
          procMap: maps.value.processed,
          mode: mode.value,
          startMs: t,
        });
        pos.value = t;

        timeline.setDuration(meta.value.durationMs);
        timeline.setGaps(gaps.value);
        if (t) timeline.setView(t - 60000, t + 60000);
      } catch (e) {
        if (seq === loadSeq) error.value = `加载失败：${e.message}`;
      } finally {
        if (seq === loadSeq) loading.value = false;
      }
    }

    function seek(ms) {
      player.seek(ms);
      pos.value = player.position();
      timeline.follow(pos.value);
      replaceQuery({ t: Math.round(pos.value) });
    }

    function toggle() {
      if (!meta.value) return;
      if (player.playing) { player.pause(); replaceQuery({ t: Math.round(player.position()) }); } else player.play();
    }

    function setMode(m) {
      if (m !== 'raw' && !hasProc.value) return;
      player.setMode(m);
      mode.value = m;
      replaceQuery({ mode: m });
    }

    function setPanel(p) {
      panel.value = p;
      replaceQuery({ panel: p });
      if (p === 'label') label.checkLs();
    }

    function onPick(item) {
      if (item.kind === 'clip') seek(item.clip.s);
      else pickEvent(item);
    }

    function pickEvent(ev) {
      selEv.value = ev.id;
      replaceQuery({ ev: ev.id });
      if (ev.mediaMs != null) seek(ev.mediaMs);
      timeline.invalidate();
    }

    const fit = () => timeline.fit();
    const downloadUrl = (track) => api.downloadUrl(ids.task, ids.step, ids.run, track);
    function onDownload() {
      dlOpen.value = false;
      toast('服务端正在拼接整段 mp4，长录像可能要等数十秒才开始下载', 'info', null, 6000);
    }

    watch(rate, (r) => player.setRate(r));
    watch(() => route.parts.join('/'), (k, old) => {
      if (route.name !== 'run') return;
      const [t, s, r] = route.parts;
      if (Number(t) === ids.task && Number(s) === ids.step && Number(r) === ids.run) return;
      if (k !== old) load();
    });
    watch([events, selEv], () => timeline && timeline.invalidate());
    // 同一个 run 内从别处带着 t / ev 跳进来（如「回放此刻」）：不重载，只定位
    watch(() => [route.query.t, route.query.ev], ([t, ev]) => {
      if (route.name !== 'run' || !meta.value) return;
      if (ev && ev !== selEv.value) selEv.value = ev;
      if (t != null && Math.abs(Number(t) - pos.value) > 500) {
        player.seek(Number(t));
        pos.value = player.position();
        timeline.centerOn(pos.value);
      }
    });

    function onKey(e) {
      if (route.name !== 'run' || !meta.value) return;
      const tag = (e.target.tagName || '').toLowerCase();
      if (['input', 'select', 'textarea'].includes(tag)) return;
      if (e.key === ' ') { e.preventDefault(); toggle(); }
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); seek(pos.value + (e.key === 'ArrowLeft' ? -1000 : 1000)); }
    }
    const closeDl = () => { dlOpen.value = false; };

    function loop() {
      if (meta.value) {
        const prev = pos.value;
        const now = player.tick();
        const wasPlaying = playing.value;
        playing.value = player.playing;
        if (wasPlaying && !playing.value) replaceQuery({ t: Math.round(now) });
        if (playing.value) {
          for (const g of gaps.value) {
            if (prev < g.mediaMs && now >= g.mediaMs) {
              gapFlash.value = g;
              setTimeout(() => { if (gapFlash.value === g) gapFlash.value = null; }, 1800);
            }
          }
          timeline.follow(now);
        }
        pos.value = now;
        procHasFrame.value = player.procHasFrame;
        drift.value = playing.value ? player.drift() : null;
        timeline.render(now);
      }
      raf = requestAnimationFrame(loop);
    }

    onMounted(async () => {
      await nextTick();
      player = new DualPlayer(rawV.value, procV.value);
      player.onError = (msg) => toast(msg, 'bad', null, 6000);
      timeline = new Timeline(tlCv.value, tlWrap.value, {
        onSeek: seek,
        onPick,
        onHover: (h) => { hover.value = h; },
        wallAt: (m) => (maps.value.raw ? maps.value.raw.wallAt(m) : null),
      });
      timeline.setLanes([eventLane(() => events.value, () => selEv.value), label.lane]);
      if (panel.value === 'label') label.checkLs();
      window.addEventListener('keydown', onKey);
      document.addEventListener('click', closeDl);
      raf = requestAnimationFrame(loop);
      load();
    });
    onBeforeUnmount(() => {
      cancelAnimationFrame(raf);
      window.removeEventListener('keydown', onKey);
      document.removeEventListener('click', closeDl);
      if (player) player.destroy();
      label.dispose();
    });

    return {
      ids, meta, gaps, events, loading, error, mode, hasProc, playing, pos, rate, drift, procHasFrame, selEv, gapFlash, hover,
      dlOpen, rawV, procV, tlCv, tlWrap, alias, wallNow, noFrameReason, panel, selEvObj, label, lsDot, lsText,
      toggle, setMode, setPanel, pickEvent, seek, canMark, fit, downloadUrl, onDownload,
      fmtDur, fmtMedia, fmtWall, fmtWallMs, lvCls,
    };
  },
};

export function runCrumbs(r) {
  const [task, step, run] = r.parts;
  const crumbs = [{ t: '任务总览', to: '/tasks' }];
  if (!task) return [...crumbs, { t: '回放' }];
  crumbs.push({ t: `任务 ${task}` }, { t: `step ${step} ${stageOf(step).alias}` });
  if (run && run !== 'latest') crumbs.push({ t: `run ${fmtWall(Number(run), true)}` });
  return crumbs;
}
