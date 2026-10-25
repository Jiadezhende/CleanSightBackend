// 级别 → 配色。只按通用级别枚举上色，未知值一律灰；文字原样显示后端的值。
const LEVEL_CLS = { critical: 'high', high: 'high', medium: 'med', warning: 'warn', low: 'warn' };
const CLS_COLOR = { high: '#e5484d', med: '#f76b15', warn: '#d4a00a', info: '#8b93a1' };

export const lvCls = (level) => LEVEL_CLS[level] || 'info';
export const lvColor = (level) => CLS_COLOR[lvCls(level)];
