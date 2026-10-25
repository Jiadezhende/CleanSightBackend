// 时间格式化：入参一律毫秒（epoch 墙钟或媒体刻度，按函数名区分）。
const pad = (n, w = 2) => String(n).padStart(w, '0');

/** 媒体刻度 → mm:ss.s（dec=false 时 mm:ss）；null 显示「整段」。 */
export function fmtMedia(ms, dec = true) {
  if (ms == null) return '整段';
  const s = Math.max(0, ms) / 1000;
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return dec ? `${pad(m)}:${sec.toFixed(1).padStart(4, '0')}` : `${pad(m)}:${pad(Math.floor(sec))}`;
}

/** epoch 毫秒 → HH:MM:SS（withDate 时 MM-DD HH:MM:SS）。 */
export function fmtWall(ms, withDate = false) {
  if (ms == null) return '—';
  const d = new Date(ms);
  const hms = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return withDate ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hms}` : hms;
}

/** epoch 毫秒 → YYYY-MM-DD HH:MM:SS.mmm */
export function fmtWallMs(ms) {
  const d = new Date(ms);
  return `${d.getFullYear()}-${fmtWall(ms, true)}.${pad(d.getMilliseconds(), 3)}`;
}

/** 时长毫秒 → 8.5s / 42s / 3m07s */
export function fmtDur(ms) {
  if (ms == null) return '—';
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m${pad(Math.round(s - m * 60))}s`;
}
