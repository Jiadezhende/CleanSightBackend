// 级别 → 配色。只按通用级别枚举上色，未知值一律灰；文字原样显示后端的值。
const LEVEL_CLS = { critical: 'high', high: 'high', medium: 'med', warning: 'warn', low: 'warn' };
const CLS_COLOR = { high: '#e5484d', med: '#f76b15', warn: '#d4a00a', info: '#8b93a1' };

export const lvCls = (level) => LEVEL_CLS[level] || 'info';

// 类别 → 颜色：按后端给的类别顺序依次取调色板，不按类名写死。
const PALETTE = ['#64748b', '#38bdf8', '#22c55e', '#a855f7', '#f472b6', '#f59e0b', '#14b8a6', '#ef4444', '#8b5cf6', '#84cc16'];

export function labelColor(labels, k) {
  const i = labels.indexOf(k);
  return i < 0 ? '#94a3b8' : PALETTE[i % PALETTE.length];
}
export const lvColor = (level) => CLS_COLOR[lvCls(level)];
