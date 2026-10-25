// 实时监控 #/live/{task}?：左侧在线 run，右侧聚焦一路的推理画面（WS，task_id 模式锁定这次 run）、近 10s 信号与告警流。
// 只有聚焦的一路连 WS（每帧约 55KB），信号与告警也只拉聚焦的一路。
import { stageOf } from '../api/catalog.js';
import { api } from '../api/endpoints.js';
import { createPoller } from '../api/http.js';
import { fmtWall } from '../core/format.js';
import { lvCls } from '../core/levels.js';
import { go, route } from '../core/router.js';

const { ref, reactive, computed, watch, onMounted, onBeforeUnmount } = Vue;

const STALL_MS = 3000;       // WS 没有心跳：末帧超过这么久没更新就提示画面停滞
const MAX_ALARMS = 50;

export const LivePage = {
  template: `
<div class="page">
  <div class="live-grid">
    <div class="card" style="align-self:start">
      <div class="card-h"><h3>在线 run</h3><span class="sub">{{ runs.length }} 路</span></div>
      <div v-for="r in runs" :key="r.task_id" class="lv-item" :class="{on: focus && focus.task_id === r.task_id}" @click="go('/live/' + r.task_id)">
        <div class="l1">
          <i class="dot live"></i><b class="mono">{{ r.task_id }}</b>
          <span class="alias">{{ stageOf(r.step_id).alias }}</span>
          <span class="mono sub">{{ r.source_ip }}</span>
        </div>
        <div class="l2 sub mono">run {{ fmtWall(r.run_id, true) }}</div>
      </div>
      <div v-if="!runs.length" class="empty">当前没有在线 run</div>
    </div>

    <div v-if="focus">
      <div class="card" style="overflow:hidden">
        <div class="screen-box"><div class="screen">
          <img v-if="frame" :src="frame" alt="" />
          <div v-if="!frame && !ended" class="ovl"><b>等待画面…</b><span>run 刚开始或推理尚未出帧</span></div>
          <div v-if="!ended && frame && sinceFrame > STALL_MS" class="stall">画面停滞 {{ (sinceFrame / 1000).toFixed(1) }}s</div>
          <div class="ovl" v-if="ended">
            <b>run 已结束</b><span>task {{ focus.task_id }} 的这次 run 已停止推流</span>
            <button class="btn primary" @click="openReplay()">打开回放</button>
          </div>
        </div></div>
        <div class="sbar mono">
          <span><i class="dot" :class="ended ? 'off' : (frame && sinceFrame > STALL_MS ? 'warn' : 'live')" style="margin-right:6px"></i>{{ ended ? '已结束' : (frame && sinceFrame > STALL_MS ? '停滞' : '直播') }}</span>
          <span>{{ fps }} fps</span>
          <span v-if="frame">末帧 {{ (sinceFrame / 1000).toFixed(1) }}s 前</span>
          <span style="margin-left:auto">task {{ focus.task_id }} · step {{ focus.step_id }} {{ stageOf(focus.step_id).alias }} · {{ focus.source_ip }}</span>
          <button class="btn sm" style="color:#111827" @click="openReplay()">打开回放</button>
        </div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="card-h"><h3>信号（近 10s）</h3></div>
        <div class="card-b">
          <div class="sigrow" v-if="Object.keys(signals).length">
            <div class="sigcard" v-for="(v, k) in signals" :key="k">
              <i class="b" :class="{on: v.active}"></i>
              <div><b class="mono">{{ k }}</b><div class="mono sub">命中 {{ v.hit_count }} · max {{ v.max_conf.toFixed(2) }}</div></div>
            </div>
          </div>
          <div v-else class="sub">本 step 没有实时信号。</div>
        </div>
      </div>

      <div class="card astream" style="margin-top:14px">
        <div class="card-h"><h3>告警流</h3><span class="sub">本 run 的实时告警（内存，最多 100 条）</span></div>
        <div v-for="a in alarms" :key="a.seq" class="r" :class="{new: a.fresh}">
          <span class="mono">{{ fmtWall(a.ts) }}</span>
          <span class="lv" :class="lvCls(a.level)">{{ a.level }}</span>
          <span class="mono">{{ a.metric }}</span>
          <span>{{ a.message }}</span>
          <a @click="openReplay(a.ts)">回放此刻 →</a>
        </div>
        <div v-if="!alarms.length" class="empty">暂无告警</div>
      </div>
    </div>
    <div v-else-if="runs.length" class="card empty">从左侧选一路</div>
  </div>
</div>`,
  setup() {
    const runs = ref([]);
    const frame = ref(null);
    const ended = ref(false);
    const fps = ref(0);
    const lastFrameAt = ref(0);
    const now = ref(Date.now());
    const signals = reactive({});
    const alarms = ref([]);
    let sinceSeq = 0;
    let ws = null;
    let frames = 0;

    const focusId = computed(() => Number(route.parts[0]) || (runs.value[0] && runs.value[0].task_id) || null);
    const focus = computed(() => runs.value.find((r) => r.task_id === focusId.value) || null);
    const sinceFrame = computed(() => (lastFrameAt.value ? now.value - lastFrameAt.value : 0));

    function closeWs() {
      if (ws) { ws.onmessage = null; ws.onclose = null; ws.close(); ws = null; }
    }

    function connect(taskId) {
      closeWs();
      frame.value = null;
      ended.value = false;
      lastFrameAt.value = 0;
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/ai/video?task_id=${taskId}`);
      ws.onmessage = (e) => {
        if (typeof e.data === 'string' && e.data.startsWith('data:')) {
          frame.value = e.data;
          lastFrameAt.value = Date.now();
          frames += 1;
          return;
        }
        try { if (JSON.parse(e.data).type === 'idle') { ended.value = true; frame.value = null; } } catch { /* 忽略非 JSON */ }
      };
    }

    function resetFocusData() {
      sinceSeq = 0;
      alarms.value = [];
      for (const k of Object.keys(signals)) delete signals[k];
    }

    watch(() => (focus.value ? focus.value.task_id : null), (id) => {
      resetFocusData();
      if (id) { connect(id); message.refresh(); } else closeWs();
    });

    const list = createPoller('/task/live', 3000, async () => { runs.value = (await api.taskLive()).tasks; });
    const message = createPoller('/task/message', 1000, async () => {
      const id = focus.value && focus.value.task_id;
      if (!id) return;
      const d = await api.taskMessage(id, sinceSeq);
      if (!focus.value || focus.value.task_id !== id) return;
      for (const k of Object.keys(signals)) if (!(k in d.signals_10s)) delete signals[k];
      Object.assign(signals, d.signals_10s);
      if (d.alarms.length) {
        const fresh = d.alarms.slice().reverse().map((a) => ({ ...a, fresh: sinceSeq > 0 }));
        alarms.value = fresh.concat(alarms.value).slice(0, MAX_ALARMS);
      }
      sinceSeq = d.max_seq;
    });

    /** ts（墙钟 ms）给了就带 at=，工作台按清单换算到媒体刻度定位。 */
    function openReplay(ts) {
      const r = focus.value;
      if (!r) return;
      go(`/run/${r.task_id}/${r.step_id}/${r.run_id}${ts ? `?panel=review&at=${ts}` : ''}`);
    }

    let tick = null;
    onMounted(() => {
      list.start();
      message.start();
      tick = setInterval(() => { now.value = Date.now(); fps.value = frames; frames = 0; }, 1000);
    });
    onBeforeUnmount(() => { list.stop(); message.stop(); clearInterval(tick); closeWs(); });

    return { runs, focus, frame, ended, fps, sinceFrame, signals, alarms, STALL_MS, openReplay, go, stageOf, fmtWall, lvCls };
  },
};

export function liveCrumbs(r) {
  return [{ t: '实时监控', to: '/live' }, ...(r.parts[0] ? [{ t: `任务 ${r.parts[0]}` }] : [])];
}
