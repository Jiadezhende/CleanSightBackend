// 双轨播放器：raw / processed 两个 <video>，按墙钟对时。
//   const p = new DualPlayer(rawVideo, procVideo);
//   p.load({ rawUrl, procUrl, rawMap, procMap, mode, startMs });   // procUrl / procMap 可为 null（只落了 raw）
//   p.setMode('sbs');  p.seek(ms);  p.toggle();  每帧调 p.tick() 推进跟随轨并取当前位置
// 位置一律用 raw 媒体刻度（时间轴坐标、送标入参同轴）。并排时 raw 是主轨：processed 按
// raw 媒体 → 墙钟 → processed 媒体 换算目标，偏差大就 seek、小就微调 playbackRate 追平。

const HARD_SYNC_MS = 150;       // 播放中跟随轨偏差超过它直接 seek
const PAUSED_SYNC_MS = 40;      // 暂停时的定位容差（约一帧）
const RELOAD_COOLDOWN_MS = 5000;

export class DualPlayer {
  constructor(rawVideo, procVideo) {
    this.v = { raw: rawVideo, processed: procVideo };
    this.hls = { raw: null, processed: null };
    this.urls = { raw: null, processed: null };
    this.maps = { raw: null, processed: null };
    this.mode = 'raw';
    this.rate = 1;
    this.procHasFrame = true;     // 并排 / processed 模式下此刻 processed 有没有画面
    this.lastReload = { raw: 0, processed: 0 };
    this.onError = () => {};
  }

  load({ rawUrl, procUrl, rawMap, procMap, mode = 'raw', startMs = 0 }) {
    this.destroy();
    this.urls = { raw: rawUrl, processed: procUrl };
    this.maps = { raw: rawMap, processed: procMap };
    this.mode = procUrl ? mode : 'raw';
    this._attach('raw', startMs / 1000);
    if (procUrl) {
      const pm = procMap.mediaAtSnapped(rawMap.wallAt(startMs));
      this._attach('processed', pm / 1000);
    }
  }

  get hasProc() { return !!this.urls.processed; }

  get master() { return this.mode === 'processed' ? 'processed' : 'raw'; }

  get playing() { return !this.v[this.master].paused; }

  /** 当前位置（raw 媒体刻度 ms）。 */
  position() {
    const { raw, processed } = this.maps;
    if (this.master === 'raw') return this.v.raw.currentTime * 1000;
    const wall = processed.wallAt(this.v.processed.currentTime * 1000);
    return raw.mediaAtSnapped(wall);
  }

  seek(ms) {
    const target = Math.max(0, Math.min(ms, this.maps.raw.durationMs));
    if (this.master === 'raw') {
      this.v.raw.currentTime = target / 1000;
    } else {
      const pm = this.maps.processed.mediaAtSnapped(this.maps.raw.wallAt(target));
      this.v.processed.currentTime = pm / 1000;
    }
    this.tick();
  }

  setMode(mode) {
    if (mode !== 'raw' && !this.hasProc) return;
    const pos = this.position();
    const wasPlaying = this.playing;
    this.v.raw.pause();
    if (this.hasProc) this.v.processed.pause();
    this.mode = mode;
    this.seek(pos);
    if (wasPlaying) this.play();
  }

  play() {
    this.v[this.master].play().catch(() => {});
    this.tick();
  }

  pause() {
    this.v.raw.pause();
    if (this.hasProc) this.v.processed.pause();
  }

  toggle() { if (this.playing) this.pause(); else this.play(); }

  setRate(r) {
    this.rate = r;
    this.v.raw.playbackRate = r;
    if (this.hasProc) this.v.processed.playbackRate = r;
  }

  /** 每帧调：并排时让 processed 跟住 raw；返回当前位置。 */
  tick() {
    const pos = this.position();
    if (!this.hasProc || this.mode === 'raw') { this.procHasFrame = true; return pos; }
    const proc = this.v.processed;
    if (this.mode === 'processed') {
      this.procHasFrame = true;
      return pos;
    }
    const target = this.maps.processed.mediaAt(this.maps.raw.wallAt(pos));
    if (target == null) {
      // 此刻 processed 没有画面（推理预热 / 断流）：停在原地，等主轨走进有画面的区间
      this.procHasFrame = false;
      if (!proc.paused) proc.pause();
      return pos;
    }
    this.procHasFrame = true;
    const diff = proc.currentTime * 1000 - target;
    // 暂停时没有追平的机会，差一帧以上就直接定位；播放中差得多才 seek，小偏差靠变速追
    if (Math.abs(diff) > (this.playing ? HARD_SYNC_MS : PAUSED_SYNC_MS)) {
      proc.currentTime = target / 1000;
    } else {
      proc.playbackRate = this.rate * Math.min(1.1, Math.max(0.9, 1 - diff / 1000));
    }
    if (this.playing && proc.paused) proc.play().catch(() => {});
    if (!this.playing && !proc.paused) proc.pause();
    return pos;
  }

  /** 两轨当前偏差（ms），只在并排且两边都有画面时有意义。 */
  drift() {
    if (this.mode !== 'sbs' || !this.hasProc || !this.procHasFrame) return null;
    const target = this.maps.processed.mediaAt(this.maps.raw.wallAt(this.position()));
    return target == null ? null : Math.round(this.v.processed.currentTime * 1000 - target);
  }

  destroy() {
    for (const k of ['raw', 'processed']) {
      if (this.hls[k]) { this.hls[k].destroy(); this.hls[k] = null; }
      this.v[k].removeAttribute('src');
      this.v[k].load();
    }
  }

  _attach(track, startSec) {
    const video = this.v[track];
    const url = this.urls[track];
    const seekOnReady = () => { if (startSec > 0) video.currentTime = startSec; };
    if (window.Hls && window.Hls.isSupported()) {
      const hls = new window.Hls({ maxBufferLength: 30, startPosition: startSec > 0 ? startSec : -1 });
      hls.on(window.Hls.Events.ERROR, (_e, data) => this._onHlsError(track, data));
      hls.loadSource(url);
      hls.attachMedia(video);
      this.hls[track] = hls;
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url;
      video.addEventListener('loadedmetadata', seekOnReady, { once: true });
    } else {
      this.onError('浏览器不支持 HLS 播放');
    }
  }

  // 段 token 有效期有限（默认 300s），过期后段请求 403：重拉清单换新 token，回到原位置
  _onHlsError(track, data) {
    const code = data.response && data.response.code;
    const expired = code === 403;
    if (!data.fatal && !expired) return;
    const now = Date.now();
    if (now - this.lastReload[track] < RELOAD_COOLDOWN_MS) {
      if (data.fatal) this.onError(`${track} 轨播放失败：${data.details}`);
      return;
    }
    this.lastReload[track] = now;
    const pos = this.v[track].currentTime;
    const wasPlaying = !this.v[track].paused;
    this.hls[track].destroy();
    this._attach(track, pos);
    if (wasPlaying) this.v[track].play().catch(() => {});
  }
}
