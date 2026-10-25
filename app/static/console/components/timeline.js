// 多泳道时间轴（canvas）：刻度尺（含断流标记）+ 若干泳道，共用 raw 媒体坐标与同一个缩放窗口。
//   const tl = new Timeline(canvas, wrap, { onSeek, onPick, onHover, wallAt });
//   tl.setDuration(ms); tl.setGaps(gaps); tl.setLanes([...]); 每帧 tl.render(posMs)
// 泳道：{ key, name, height, draw(c, tl, lane), hit?(x, tl) → item, tip?(x, m, tl) → string[],
//         empty?() → string, create?: true（在该泳道上拖选会回调 onCreate） }
// 数据变了调 tl.invalidate()：静态层（刻度 / 泳道内容）只在失效时重画，播放头每帧画。

import { fmtDur, fmtMedia, fmtWall } from '../core/format.js';

export const LBL = 92;   // 左侧泳道名列宽
const PR = 10;
const RULER_H = 32;
const MIN_SPAN_MS = 4000;

export class Timeline {
  constructor(canvas, wrap, handlers) {
    this.cv = canvas;
    this.wrap = wrap;
    this.h = handlers;
    this.view = { v0: 0, v1: 1 };
    this.duration = 1;
    this.gaps = [];
    this.lanes = [];
    this.layout = [];
    this.W = 0;
    this.H = 0;
    this.dirty = true;
    this.staticCv = document.createElement('canvas');
    this.hoverX = null;
    this.drag = null;
    this._bind();
  }

  // ── 数据 ─────────────────────────────────────────────
  setDuration(ms) { this.duration = Math.max(1, ms); this.fit(); }
  setGaps(gaps) { this.gaps = gaps; this.invalidate(); }
  setLanes(lanes) { this.lanes = lanes; this.invalidate(); }
  invalidate() { this.dirty = true; }

  // ── 坐标与视窗 ───────────────────────────────────────
  inner() { return Math.max(1, this.W - LBL - PR); }
  m2x(m) { return LBL + (m - this.view.v0) / (this.view.v1 - this.view.v0) * this.inner(); }
  x2m(x) { return this.view.v0 + (x - LBL) / this.inner() * (this.view.v1 - this.view.v0); }
  clampMs(m) { return Math.min(this.duration, Math.max(0, m)); }

  setView(a, b) {
    const span = Math.min(this.duration, Math.max(MIN_SPAN_MS, b - a));
    const v0 = Math.min(this.duration - span, Math.max(0, a));
    if (v0 === this.view.v0 && v0 + span === this.view.v1) return;
    this.view = { v0, v1: v0 + span };
    this.invalidate();
    if (this.h.onView) this.h.onView(this.view);
  }
  fit() { this.setView(0, this.duration); }
  zoomBy(k, m) {
    const span = (this.view.v1 - this.view.v0) * k;
    const r = Math.min(1, Math.max(0, (m - this.view.v0) / (this.view.v1 - this.view.v0)));
    this.setView(m - r * span, m - r * span + span);
  }
  centerOn(m) {
    const span = this.view.v1 - this.view.v0;
    this.setView(m - span / 2, m + span / 2);
  }
  /** 播放时播放头走出视窗右缘就翻页。 */
  follow(m) {
    const span = this.view.v1 - this.view.v0;
    if (m > this.view.v1 - span * 0.03 && this.view.v1 < this.duration) this.setView(m - span * 0.1, m + span * 0.9);
    else if (m < this.view.v0) this.centerOn(m);
  }

  laneAt(y) { return this.layout.find((l) => y >= l.y && y < l.y + l.h); }

  // ── 绘制 ─────────────────────────────────────────────
  render(pos) {
    const W = this.wrap.clientWidth;
    let y = RULER_H;
    this.layout = [{ key: 'ruler', y: 0, h: RULER_H }];
    for (const lane of this.lanes) {
      const h = typeof lane.height === 'function' ? lane.height() : lane.height;
      this.layout.push({ ...lane, y, h });
      y += h;
    }
    const H = y;
    const dpr = window.devicePixelRatio || 1;
    if (this.cv.width !== Math.round(W * dpr) || this.cv.height !== Math.round(H * dpr)) {
      this.cv.width = this.staticCv.width = Math.round(W * dpr);
      this.cv.height = this.staticCv.height = Math.round(H * dpr);
      this.cv.style.height = `${H}px`;
      this.dirty = true;
    }
    this.W = W;
    this.H = H;
    if (this.dirty) {
      const sc = this.staticCv.getContext('2d');
      sc.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.dirty = false;            // 先清：泳道在 draw 里 invalidate()（如闪烁动画）表示下一帧还要重画
      this._drawStatic(sc);
    }
    const c = this.cv.getContext('2d');
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, this.cv.width, this.cv.height);
    c.drawImage(this.staticCv, 0, 0);
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (this.hoverX != null && this.hoverX > LBL) {
      c.fillStyle = 'rgba(17,24,39,.15)';
      c.fillRect(this.hoverX, 0, 1, H);
    }
    const x = this.m2x(pos);
    if (x >= LBL - 1 && x <= W - PR + 1) {
      c.fillStyle = '#111827';
      c.fillRect(x - 0.75, 6, 1.5, H - 6);
      c.beginPath(); c.moveTo(x - 6, 0); c.lineTo(x + 6, 0); c.lineTo(x, 8); c.closePath(); c.fill();
    }
  }

  _drawStatic(c) {
    const { W, H } = this;
    c.clearRect(0, 0, W, H);
    this.layout.forEach((l, i) => {
      c.fillStyle = l.key === 'ruler' ? '#f8f9fb' : (i % 2 ? '#ffffff' : '#fbfcfd');
      c.fillRect(0, l.y, W, l.h);
      c.fillStyle = '#eef0f3';
      c.fillRect(0, l.y + l.h - 1, W, 1);
      if (l.name) {
        c.fillStyle = '#6b7280'; c.font = '12px system-ui'; c.textBaseline = 'middle';
        c.fillText(typeof l.name === 'function' ? l.name() : l.name, 12, l.y + l.h / 2);
      }
    });
    c.fillStyle = '#e5e7eb';
    c.fillRect(LBL - 1, 0, 1, H);

    c.save();
    c.beginPath(); c.rect(LBL, 0, W - LBL - PR, H); c.clip();
    this._drawRuler(c);
    for (const l of this.layout) if (l.draw) l.draw(c, this, l);
    c.restore();

    c.font = '12px system-ui'; c.fillStyle = '#9ca3af'; c.textBaseline = 'middle';
    for (const l of this.layout) {
      const txt = l.empty && l.empty();
      if (txt) c.fillText(txt, LBL + 12, l.y + l.h / 2 + 1);
    }
  }

  _drawRuler(c) {
    const { H } = this;
    const span = this.view.v1 - this.view.v0;
    const ppm = this.inner() / span;
    const stp = [1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000].find((s) => s * ppm >= 84) || 600000;
    const minor = stp / 5;
    c.textBaseline = 'alphabetic';
    for (let m = Math.ceil(this.view.v0 / minor) * minor; m <= this.view.v1; m += minor) {
      const x = Math.round(this.m2x(m)) + 0.5;
      const major = Math.round(m) % stp === 0;
      c.fillStyle = major ? '#9ca3af' : '#d1d5db';
      c.fillRect(x, major ? 18 : 25, 1, major ? 14 : 7);
      if (major) {
        c.fillStyle = '#f0f2f5'; c.fillRect(x, RULER_H, 1, H - RULER_H);
        c.fillStyle = '#6b7280'; c.font = '11px Consolas,monospace';
        c.fillText(fmtMedia(m, stp < 5000), x + 4, 13);
      }
    }
    // 断流：在媒体轴上宽度为零，画成刻度尺上的红色标签 + 贯穿各泳道的红线
    for (const g of this.gaps) {
      const x = Math.round(this.m2x(g.mediaMs)) + 0.5;
      c.fillStyle = 'rgba(239,68,68,.55)';
      c.fillRect(x - 0.5, RULER_H, 1.5, H - RULER_H);
      const txt = `断流 ${fmtDur(g.gapMs)}`;
      c.font = '600 10.5px system-ui';
      const w = c.measureText(txt).width + 10;
      c.fillStyle = '#dc2626';
      roundRect(c, x - w / 2, 17, w, 14, 3); c.fill();
      c.fillStyle = '#fff'; c.textBaseline = 'middle';
      c.fillText(txt, x - w / 2 + 5, 24.5);
      c.textBaseline = 'alphabetic';
    }
  }

  // ── 交互 ─────────────────────────────────────────────
  _pos(e) {
    const b = this.cv.getBoundingClientRect();
    return { x: e.clientX - b.left, y: e.clientY - b.top };
  }

  gapAt(x) { return this.gaps.find((g) => Math.abs(this.m2x(g.mediaMs) - x) < 30); }

  _bind() {
    const cv = this.cv;
    cv.addEventListener('pointerdown', (e) => this._down(e));
    cv.addEventListener('pointermove', (e) => this._move(e));
    cv.addEventListener('pointerup', (e) => this._up(e));
    cv.addEventListener('pointerleave', () => { this.hoverX = null; this.h.onHover(null); });
    cv.addEventListener('wheel', (e) => this._wheel(e), { passive: false });
  }

  _down(e) {
    const p = this._pos(e);
    const l = this.laneAt(p.y);
    if (!l) return;
    try { this.cv.setPointerCapture(e.pointerId); } catch { /* 合成事件无 pointer */ }
    if (p.x < LBL) { if (l.onLabelClick) l.onLabelClick(); return; }
    if (l.onPointerDown) {
      const d = l.onPointerDown(p.x, this);
      if (d) { this.drag = { ...d, lane: l, x0: p.x, moved: false }; return; }
    }
    this.drag = { type: 'pan', lane: l, x0: p.x, v0: this.view.v0, v1: this.view.v1, moved: false };
  }

  _move(e) {
    const p = this._pos(e);
    this._hover(p);
    const d = this.drag;
    if (!d) return;
    if (Math.abs(p.x - d.x0) > 3) d.moved = true;
    if (!d.moved) return;
    if (d.type === 'pan') {
      const dm = (p.x - d.x0) / this.inner() * (d.v1 - d.v0);
      this.setView(d.v0 - dm, d.v1 - dm);
    } else if (d.lane.onDrag) {
      d.lane.onDrag(d, p.x, this);
    }
  }

  _up(e) {
    const p = this._pos(e);
    const d = this.drag;
    this.drag = null;
    if (!d) return;
    if (d.type !== 'pan' && d.moved && d.lane.onDragEnd) { d.lane.onDragEnd(d, p.x, this); return; }
    if (d.moved) return;
    const item = d.lane.hit && d.lane.hit(p.x, this);
    if (item && this.h.onPick) { this.h.onPick(item); return; }
    this.h.onSeek(this.clampMs(this.x2m(p.x)));
  }

  _wheel(e) {
    e.preventDefault();
    const p = this._pos(e);
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      const dm = (e.deltaX || e.deltaY) / this.inner() * (this.view.v1 - this.view.v0);
      this.setView(this.view.v0 + dm, this.view.v1 + dm);
      return;
    }
    this.zoomBy(Math.exp(e.deltaY * 0.0015), this.clampMs(this.x2m(Math.max(LBL, p.x))));
  }

  _hover(p) {
    const l = this.laneAt(p.y);
    if (!l || p.x < LBL) {
      this.hoverX = null;
      this.cv.style.cursor = l && p.x < LBL && l.onLabelClick ? 'pointer' : 'default';
      this.h.onHover(null);
      return;
    }
    this.hoverX = p.x;
    const m = this.clampMs(this.x2m(p.x));
    const wall = this.h.wallAt ? this.h.wallAt(m) : null;
    const lines = [`${fmtMedia(m, true)}${wall != null ? `  ·  墙钟 ${fmtWall(wall)}` : ''}`];
    let cursor = l.cursor ? l.cursor(p.x, this) : 'default';
    if (l.key === 'ruler') {
      const g = this.gapAt(p.x);
      if (g) lines.push(`断流 ${fmtDur(g.gapMs)}：${fmtWall(g.wallFromMs)} → ${fmtWall(g.wallToMs)}`, '跨过此点的片段送标会失败');
    }
    if (l.tip) {
      const extra = l.tip(p.x, m, this);
      if (extra && extra.length) { lines.push(...extra); if (l.hit && l.hit(p.x, this)) cursor = 'pointer'; }
    }
    this.cv.style.cursor = cursor;
    const tx = p.x + 14 > this.W - 300 ? p.x - 310 : p.x + 14;
    const ty = l.y < 60 ? l.y + l.h + 4 : l.y - (lines.length * 18 + 18);
    this.h.onHover({ x: Math.max(4, tx), y: Math.max(4, ty), lines });
  }
}

// ── 小工具 ───────────────────────────────────────────
export function roundRect(c, x, y, w, h, r) {
  c.beginPath();
  c.moveTo(x + r, y);
  c.arcTo(x + w, y, x + w, y + h, r);
  c.arcTo(x + w, y + h, x, y + h, r);
  c.arcTo(x, y + h, x, y, r);
  c.arcTo(x, y, x + w, y, r);
  c.closePath();
}
