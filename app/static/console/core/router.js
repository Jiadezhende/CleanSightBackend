// hash 路由：#/<name>/<parts...>?<query>。可分享的页面状态都进 URL，刷新 / 转发链接回到同一画面。
//   go('/run/127/2/latest')          跳转（会过离开保护）
//   replaceQuery({ panel: 'label' }) 改 query 不产生历史记录
//   setLeaveGuard(next => '有 2 段未提交…')  返回非空串 = 离开前要确认
import { confirmBox } from './ui.js';

const { reactive } = Vue;

export const route = reactive({ name: 'tasks', parts: [], query: {} });

function parse(hash) {
  const raw = (hash || '').replace(/^#/, '') || '/tasks';
  const [p, q = ''] = raw.split('?');
  const parts = p.split('/').filter(Boolean).map(decodeURIComponent);
  return { name: parts[0] || 'tasks', parts: parts.slice(1), query: Object.fromEntries(new URLSearchParams(q)) };
}

function hashOf(name, parts, query) {
  const q = new URLSearchParams(Object.entries(query).filter(([, v]) => v != null && v !== '')).toString();
  return '#/' + [name, ...parts].map(encodeURIComponent).join('/') + (q ? `?${q}` : '');
}

let lastHash = location.hash;
let bypass = false;
let leaveGuard = null;

export function go(path) { location.hash = path; }

export function replaceQuery(patch) {
  const query = { ...route.query, ...patch };
  for (const k of Object.keys(query)) if (query[k] == null || query[k] === '') delete query[k];
  const h = hashOf(route.name, route.parts, query);
  history.replaceState(null, '', h);
  lastHash = h;
  route.query = query;
}

/** 换路径（保留 query），不产生历史记录；用于把 latest 落成具体 run_id。 */
export function replacePath(path) {
  const next = parse(`#${path}`);
  const h = hashOf(next.name, next.parts, route.query);
  history.replaceState(null, '', h);
  lastHash = h;
  Object.assign(route, parse(h));
}

export function setLeaveGuard(fn) { leaveGuard = fn; }

async function onHashChange() {
  const next = parse(location.hash);
  const message = !bypass && leaveGuard ? leaveGuard(next) : null;
  if (message) {
    const target = location.hash;
    history.replaceState(null, '', lastHash);
    const ok = await confirmBox({ title: '离开当前页面？', body: message, okText: '仍要离开', cancelText: '留下' });
    if (!ok) return;
    bypass = true;
    location.hash = target;
    return;
  }
  bypass = false;
  lastHash = location.hash;
  Object.assign(route, next);
}

window.addEventListener('hashchange', onHashChange);
window.addEventListener('beforeunload', (e) => {
  if (leaveGuard && leaveGuard(null)) { e.preventDefault(); e.returnValue = ''; }
});
Object.assign(route, parse(location.hash));
