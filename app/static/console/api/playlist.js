// 浏览器清单（/traceback/.../playlist.m3u8）→ 媒体轴 ↔ 墙钟对照表。
//   const map = parsePlaylist(text);
//   map.wallAt(mediaMs)   该轨第 mediaMs 毫秒拍于哪个墙钟时刻
//   map.mediaAt(wallMs)   某墙钟时刻在该轨的第几毫秒；该轨此刻没有画面（未开始 / 断流）返回 null
// 逐段用 #EXT-X-PROGRAM-DATE-TIME（段墙钟起点）+ EXTINF（段媒体时长）换算；
// 「首段墙钟 + currentTime」只在从没断过流时成立，不能用。媒体轴段起点 = 之前各段 EXTINF（取整到 ms）之和，与后端同口径。

const PDT = '#EXT-X-PROGRAM-DATE-TIME:';
const EXTINF = '#EXTINF:';

export function parsePlaylist(text) {
  const segs = [];
  let wall = null;
  let media = 0;
  for (const line of text.split(/\r?\n/)) {
    if (line.startsWith(PDT)) {
      wall = Date.parse(line.slice(PDT.length));
    } else if (line.startsWith(EXTINF)) {
      const dur = Math.round(parseFloat(line.slice(EXTINF.length)) * 1000);
      segs.push({ mediaMs: media, wallMs: Number.isFinite(wall) ? wall : null, durMs: dur });
      media += dur;
      wall = null;
    }
  }
  return new MediaMap(segs);
}

export class MediaMap {
  constructor(segs) {
    this.segs = segs;
    this.durationMs = segs.length ? segs[segs.length - 1].mediaMs + segs[segs.length - 1].durMs : 0;
    this.hasWall = segs.length > 0 && segs.every((s) => s.wallMs != null);
  }

  /** 媒体刻度 → 墙钟；超出范围贴边。 */
  wallAt(mediaMs) {
    const s = this._segAtMedia(mediaMs);
    if (!s) return null;
    return s.wallMs + Math.min(Math.max(0, mediaMs - s.mediaMs), s.durMs);
  }

  /** 墙钟 → 媒体刻度；落在本轨没有画面的时间（首段之前、断流空洞、末段之后）返回 null。 */
  mediaAt(wallMs) {
    const i = this._lastStartingBefore(wallMs);
    if (i < 0) return null;
    const s = this.segs[i];
    return wallMs < s.wallMs + s.durMs ? s.mediaMs + (wallMs - s.wallMs) : null;
  }

  /** 同 mediaAt，但空洞里吸附到下一段段首、超出范围贴边（与后端 media_ms_at 同规则）。 */
  mediaAtSnapped(wallMs) {
    if (!this.segs.length) return 0;
    const i = this._lastStartingBefore(wallMs);
    if (i < 0) return 0;
    const s = this.segs[i];
    if (wallMs < s.wallMs + s.durMs) return s.mediaMs + (wallMs - s.wallMs);
    return i + 1 < this.segs.length ? this.segs[i + 1].mediaMs : this.durationMs;
  }

  _segAtMedia(mediaMs) {
    if (!this.segs.length) return null;
    let lo = 0;
    let hi = this.segs.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (this.segs[mid].mediaMs <= mediaMs) lo = mid; else hi = mid - 1;
    }
    return this.segs[lo];
  }

  _lastStartingBefore(wallMs) {
    let lo = 0;
    let hi = this.segs.length - 1;
    let ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.segs[mid].wallMs <= wallMs) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }
}
