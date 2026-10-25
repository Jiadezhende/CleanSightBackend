// 唯一出网层：页面不直接调 fetch。失败统一抛 ApiError；轮询的连续失败计入顶栏健康灯。
const { reactive, computed } = Vue;

const FAIL_LIMIT = 3;   // 同一轮询连续失败到这个次数，健康灯变红

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.status = status;   // 0 = 网络不可达
    this.body = body;
  }
}

function errorMessage(status, body) {
  if (body == null || body === '') return `HTTP ${status}`;
  if (typeof body === 'string') return body.slice(0, 200);
  const d = body.detail ?? body.error ?? body.message;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join('；');   // 422 校验体
  if (d && typeof d === 'object') return d.detail || d.error || JSON.stringify(d);
  return d == null ? JSON.stringify(body) : String(d);
}

export async function request(method, path, { query, body, signal } = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(query || {})) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  }
  let resp;
  try {
    resp = await fetch(url, {
      method, signal, cache: 'no-store',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError(0, `后端不可达（${e.message || e}）`, null);
  }
  const text = await resp.text();
  let data = text;
  if ((resp.headers.get('content-type') || '').includes('json')) {
    try { data = JSON.parse(text); } catch { /* 保留原文 */ }
  }
  if (!resp.ok) throw new ApiError(resp.status, errorMessage(resp.status, data), data);
  return data;
}

export const get = (path, query) => request('GET', path, { query });
export const post = (path, body) => request('POST', path, { body });
export const put = (path, body) => request('PUT', path, { body });

// ── 轮询 + 健康灯 ──────────────────────────────────────────────────────────────
// pollers：名字 → { name, every, fails, lastError }；页面卸载时 stop() 删掉自己的条目。
export const health = reactive({ pollers: {}, lastOkAt: Date.now() });
export const backendOk = computed(() => Object.values(health.pollers).every((p) => p.fails < FAIL_LIMIT));
export const backendError = computed(() => {
  const bad = Object.values(health.pollers).find((p) => p.fails >= FAIL_LIMIT);
  return bad ? `${bad.name} 连续 ${bad.fails} 次失败：${bad.lastError}` : '';
});

/**
 * 页面可见时每 intervalMs 调一次 fn；同一时刻只有一个在途请求。
 * const p = createPoller('/admin/overview', 3000, async () => { ... }); p.start(); ... p.stop();
 */
export function createPoller(name, intervalMs, fn) {
  let timer = null;
  let inflight = false;

  async function tick() {
    if (inflight || document.hidden || timer === null) return;
    inflight = true;
    try {
      await fn();
      const p = health.pollers[name];
      if (p) { p.fails = 0; p.lastError = ''; }
      health.lastOkAt = Date.now();
    } catch (e) {
      const p = health.pollers[name];
      if (p) { p.fails += 1; p.lastError = e.message; }
    } finally {
      inflight = false;
    }
  }
  const onVisible = () => { if (!document.hidden) tick(); };

  return {
    start() {
      if (timer !== null) return;
      health.pollers[name] = { name, every: intervalMs, fails: 0, lastError: '' };
      timer = setInterval(tick, intervalMs);
      document.addEventListener('visibilitychange', onVisible);
      tick();
    },
    stop() {
      clearInterval(timer);
      timer = null;
      document.removeEventListener('visibilitychange', onVisible);
      delete health.pollers[name];
    },
    refresh: tick,
  };
}
