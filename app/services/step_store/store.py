"""
`(task_id, step_id)` → 这个 step 的一切。本包的对外门面。

对外契约一句话：**调用方只给两个 id，本包回答一切；路径不是对外概念。**

    Step        绑定一次路由键的句柄。读与写的成组能力都挂在它上面
    SegmentRef  一个段的不可变值对象（filename + ts_us）
    模块级函数   step / steps / tasks / purge_step / sweep_empty_tasks —— 无身份的
                取句柄与枚举动作。曾是 `StepStore` 类的五个方法，但那个类的唯一字段
                是存储根、五个方法全是纯转发，**没有状态可言**；而它为了把根传下去，
                在 7 个消费类里各长出一个只为测试存在的 `store=` 构造参数。故降为函数，
                存储根一律自解析（测试用 conftest 的 `tmp_storage` fixture 指临时根）。

导入约定：**函数一律模块限定，绝不裸导入。**

    from app.services.step_store import store as step_store
    step = step_store.step(task_id, step_id)

理由不是风格：`step` / `steps` / `tasks` 在调用方是高频局部变量名，裸导入后一句
`steps = steps(task_id)` 就会在函数内遮蔽同名全局，同一函数里再调一次直接
`UnboundLocalError`。类型与异常（`Step` / `SegmentRef` / `StepInitMissing` …）不与
局部变量撞名，按名导入即可。

> 「不出路径」≠「不出 `Path`」。ffmpeg 必须吃真路径（`cwd` + basename 是 4.x/8.x
> 唯一兼容写法），硬包起来就是无知识可承载的包装。**禁的是调用方自己拼**，不是禁
> `Path` 出现在返回值里 —— 故本模块不提供任何返回**目录**的成员：目录 = 根 + 两级 id
> 的拼装公式，交出去等于把布局复制一份到调用方，且门禁抓不到（`dir / "x"` 是普通
> Path 拼接，不是 settings 访问）。要 ffmpeg `cwd` 取 `scratch_path().parent`。

落盘约定（命名真源在 [layout.py](layout.py)，本模块不自建）：
    {base_dir}/{task_id}/{step_id}/{raw|processed}_segment_{ts_us}.mp4
"""

from __future__ import annotations

import bisect
import logging
import secrets
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    IO,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from app.services.step_store import layout, playlist, purge

if TYPE_CHECKING:  # 仅类型标注：Frame 属 domain（L0），但 segment_decoder 吃 numpy
    from app.domain.frame import Frame

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 领域异常：表达「这个 step 播不了」，映射成什么状态码由调用方决定
# ---------------------------------------------------------------------------


class StepStoreError(Exception):
    """本包领域异常基类。"""


class StepInitMissing(StepStoreError):
    """该轨缺 fMP4 init 段，fragment 无法解码。

    正常落盘的 step 必有 init（首段 transcode 时产出）。缺 init 只剩两种可能：
    ① 旧格式产物（不支持、无迁移路径）；② 首段仍在 transcode（窗口极短）。
    **两者服务端都无法自愈**，故调用方应按「此 step 不可播放/导出」处理。
    """


class StepNoPlayableSegments(StepStoreError):
    """该轨没有已完成落盘的段（无段，或全部在途）。"""


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


class SegmentRef(NamedTuple):
    """单个 HLS 段的引用。**只带调用方拿不到的东西。**

    刻意不带的字段与理由：
        task_id / step_id  调用方持 `Step` 句柄就有。曾经带过，结果是调用点相邻两行
                           一个从 ref 取、一个从请求路径取，同一个值绕了一圈
        track              零消费方（包内 SegmentDecoder 用构造时传入的那个）
        path               两个消费方实际都只用 `.name`，即 `filename`
        is_trigger         那是查询上下文不是段属性，改由 `segments_around` 出下标
        duration           `vod_playlist` 收口后包外无消费方；EXTINF 留在包内
    """

    filename: str
    ts_us: int

    @property
    def ts_ms(self) -> int:
        """段开始时间戳（毫秒）"""
        return self.ts_us // 1000

    @property
    def ts_s(self) -> float:
        """段开始时间戳（秒，浮点）"""
        return self.ts_us / 1_000_000.0


def _dir_name_to_int(name: str) -> Optional[int]:
    """目录名转 int；非数字（如 `.lab_exports`）返回 None。"""
    try:
        return int(name)
    except (TypeError, ValueError):
        return None


def _locate_containing_index(seg_ts_us: Sequence[int], target_us: float) -> int:
    """段起始 ts 升序数组中，**包含** target_us 的那一段的下标；全都更晚时返回 -1。

    定义：最大的 i 满足 `seg_ts_us[i] <= target_us`。

    ⚠ **必须是 `bisect_right - 1`，不能用 `bisect_left`**：
    ① `bisect_left` 取到的是 target 之**后**的段；
    ② 段文件名的 `ts_us = int(ts*1e6)` 是**截断**值，故「target 恰为该段首帧」时
       `target_us > ts_us`，`bisect_left` 同样会跳过该段。
    即不存在「大部分情况下对」，用 left 是无条件错。

    返回 -1 表示 target 早于首段起点。**调用方自行决定怎么处理**：段级区间裁剪要靠
    它表达空区间（clamp 成 0 会把空区间误判成命中第 0 段），而告警取证要 clamp 到
    首段（取最近的可用段）。这两种需求相反，故本函数不替调用方做决定。

    `target_us` 接受浮点：`seg_ts_us` 元素为整数时，`a <= t ⟺ a <= floor(t)`，
    故传 `ts*1e6` 原值与传截断值等价，调用方不必自行取整。

    **包内私有**：两个消费方（本模块的 `segments_around`、`segment_decoder.iter`）
    都在包内，off-by-one 的论证只需写这一处。
    """
    return bisect.bisect_right(seg_ts_us, target_us) - 1


# ---------------------------------------------------------------------------
# Step —— 绑定一次路由键的句柄
# ---------------------------------------------------------------------------


class Step:
    """一个 `(task_id, step_id)` 落盘单元的句柄。

    惰性求值：目录扫描（单次 `iterdir` 出双轨段）与逐轨 playlist 解析在首次访问时
    做一遍，之后在**本对象生命周期内**缓存 —— 读侧成组用这些能力（traceback 一个
    请求要段列表 + EXTINF + init 判据 + 时间跨度四项），拆成四个独立入口就是扫四遍盘。

    ⚠ **不得跨请求持有**：活跃 step 每 ~10s 落一个新段，缓存会过期。每次请求/每轮
    扫描重新 `step_store.step(...)` —— 构造本身零成本（不碰盘、不读 settings）。

    命名规则（见抽包记录 §9「连带的正名」）：property = 零成本或已随目录扫描缓存；method = 至少
    一次 I/O。动词只留给变盘的成员，查询无论多贵都用名词（`frames()` 起 ffmpeg 但
    不变盘，仍是名词）；`has_`/`is_`/`find_` 三个惯例前缀不受此限。
    """

    __slots__ = ("task_id", "step_id", "_base_dir", "_by_track", "_durations")

    def __init__(self, task_id: int, step_id: int):
        self.task_id = int(task_id)
        self.step_id = int(step_id)
        self._base_dir: Optional[Path] = None
        self._by_track: Optional[Dict[str, List[SegmentRef]]] = None
        self._durations: Dict[str, Dict[str, float]] = {}

    def __repr__(self) -> str:  # 日志与断言可读性
        return f"Step(task_id={self.task_id}, step_id={self.step_id})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Step):
            return NotImplemented
        return (self.task_id, self.step_id) == (other.task_id, other.step_id)

    def __hash__(self) -> int:
        return hash((self.task_id, self.step_id))

    # ---------------- 包内私有：目录与缓存 ----------------

    @property
    def _root(self) -> Path:
        """存储根。**首次碰盘时才解析并缓存**。

        惰性是契约的一部分：`steps()` 一次可构造上百个句柄，构造期解析等于每个句柄
        都走一遍 settings + `Path.resolve()`，破坏「构造零成本、不碰盘」。
        """
        if self._base_dir is None:
            self._base_dir = storage_root()
        return self._base_dir

    @property
    def _dir(self) -> Path:
        """step 目录。**包内私有**，绝不出包（见模块 docstring）。"""
        return layout.step_dir(self._root, self.task_id, self.step_id)

    def _scan(self) -> Dict[str, List[SegmentRef]]:
        """单次 iterdir 扫出该 step 目录下按轨分组的段（各轨内按 ts_us 升序）。

        双轨一起扫（`tracks` 要，`time_bounds_us` 要）只付一次目录遍历。
        目录不存在时返回各轨空列表。
        """
        if self._by_track is not None:
            return self._by_track

        by_track: Dict[str, List[SegmentRef]] = {t: [] for t in layout.VALID_TRACKS}
        step_path = self._dir
        if step_path.is_dir():
            for entry in step_path.iterdir():
                if not entry.is_file():
                    continue
                parsed = layout.parse_segment_name(entry.name)
                if parsed is None:
                    continue
                track, ts_us = parsed
                by_track[track].append(SegmentRef(filename=entry.name, ts_us=ts_us))
            for refs in by_track.values():
                refs.sort(key=lambda r: r.ts_us)

        self._by_track = by_track
        return by_track

    def _extinf(self, track: str) -> Dict[str, float]:
        """该轨 filename → EXTINF 时长映射（缓存）。

        EXTINF 是段时长唯一真值（不能用文件名 ts 差重推）；键集合同时是「已完成
        transcode+append」的判据，不在其中的是在途段。
        """
        cached = self._durations.get(track)
        if cached is None:
            cached = playlist.parse_playlist_durations(
                self._dir / layout.playlist_name(track)
            )
            self._durations[track] = cached
        return cached

    @staticmethod
    def _check_track(track: str) -> str:
        if track not in layout.VALID_TRACKS:
            raise ValueError(
                f"Invalid track: {track!r}, expected one of {layout.VALID_TRACKS}"
            )
        return track

    # ---------------- 读：清单与时间 ----------------

    @property
    def tracks(self) -> Tuple[str, ...]:
        """该 step **实际落盘**的轨，按 ("raw", "processed") 顺序。

        大屏按 track 默认 processed 打 playlist，只落了 raw 的 step 会 404 —— 故
        清单必须如实报有哪些轨，不能假定双轨。
        """
        by_track = self._scan()
        return tuple(t for t in layout.VALID_TRACKS if by_track[t])

    @property
    def time_bounds_us(self) -> Optional[Tuple[int, int]]:
        """双轨并集的 (最早段起点, 最晚段终点)，微秒；无可用段时 None。

        **终点取 max(seg.ts + EXTINF) 而非 max(seg.ts)** —— 后者会漏掉最后一段自身
        长度。EXTINF 是 hls.js / fragment 媒体时长的同源真值，对齐到它前端时长才与
        `<video>.duration` 一致。在途段查不到 EXTINF，跳过。

        双轨取并集：两轨段边界不一定对齐（实测有过 20+ 秒差），故它表达「该 step
        有画面的时间跨度」，不等于任一单轨的播放范围。
        """
        start_us: Optional[int] = None
        end_us: Optional[int] = None
        for track in layout.VALID_TRACKS:
            by_name = self._extinf(track)
            if not by_name:
                continue
            for s in self._scan()[track]:
                dur = by_name.get(s.filename)
                if dur is None:
                    continue
                seg_end_us = s.ts_us + int(round(dur * 1_000_000))
                if start_us is None or s.ts_us < start_us:
                    start_us = s.ts_us
                if end_us is None or seg_end_us > end_us:
                    end_us = seg_end_us
        if start_us is None or end_us is None:
            return None
        return start_us, end_us

    @property
    def last_activity_at(self) -> Optional[float]:
        """最后一次产出的时刻（unix 秒）；无任何**已登记产物**时 None。

        判据是已登记产物的 mtime 最大值，不是目录 mtime、也不是 `metadata.json` 的
        `updated_at`（那是 HLS 独有产物）—— 论证见 `purge.last_activity`。

        供 TTL 回收判断「这目录还活着吗」。**「该不该删」不在本包**，在
        `persistence/workers/cleanup_worker.py`。
        """
        return purge.last_activity(self._dir)

    def segments(self, track: str, playable_only: bool = True) -> List[SegmentRef]:
        """该轨全部段，ts 升序。

        Args:
            track: "raw" 或 "processed"
            playable_only: **默认 True，滤掉在途段** —— 在途段 = mp4 已落盘（故扫得
                到）但 transcode+append 未完成（故不在 playlist 里）。放它们过去会让
                fragment 实际媒体时长与 playlist 声明对不上：回放侧是 hls.js MSE 缓冲
                洞，导出侧是时长错乱。
                传 False 表示「磁盘上有什么就报什么」—— 清单类接口（大屏历史、lab 任务
                列表）要的是这个：它们只报有没有画面、什么时候有，不解码也不拼 m3u8。

        Raises:
            ValueError: track 非法
        """
        self._check_track(track)
        segs = self._scan()[track]
        if not playable_only:
            return list(segs)
        durations = self._extinf(track)
        return [s for s in segs if s.filename in durations]

    def segments_around(
        self,
        ts_ms: int,
        track: str,
        before: int = 1,
        after: int = 2,
    ) -> Tuple[List[SegmentRef], int]:
        """定位包含 ts_ms 的段并附带前后上下文。

        Args:
            ts_ms: 目标时间戳（毫秒，与 clean_alarm.detected_at 单位一致）
            track: "raw" 或 "processed"
            before / after: 触发段前后各附带几段

        Returns:
            `(段列表, 触发段在该列表里的下标)`，段按 ts_us 升序。无段时返回 `([], -1)`。
            `ts_ms` 早于首段时触发段取首段（取最近的可用段）。

        **出下标而非在段上打 `is_trigger` 标记**：那是查询上下文不是段属性，挂到值对象
        上会逼着调用方为设一个 bool 重建整批 frozen 副本。

        这里刻意**不滤在途段**：告警取证要的是「那一刻的画面在哪个文件里」，滤掉会让
        刚落盘的告警取不到证据。拼 m3u8 时 `vod_playlist` 会再滤一道。
        """
        if before < 0 or after < 0:
            raise ValueError("before/after must be >= 0")

        all_segs = self.segments(track, playable_only=False)
        if not all_segs:
            return [], -1

        trigger_idx = _locate_containing_index(
            [s.ts_us for s in all_segs], int(ts_ms) * 1000
        )
        if trigger_idx < 0:
            trigger_idx = 0  # ts_ms 早于首段起点 → 取首段作为最近的触发段

        lo = max(0, trigger_idx - before)
        hi = min(len(all_segs), trigger_idx + after + 1)  # +1 因为 slice 不含 end
        return all_segs[lo:hi], trigger_idx - lo

    def has_init(self, track: str) -> bool:
        """该轨的 fMP4 init 段是否已就位。

        判据是格式知识，缺了映射成什么错误（HTTP 503 / 领域异常）由调用方决定。
        理由见 `StepInitMissing`。
        """
        self._check_track(track)
        return (self._dir / layout.init_name(track)).exists()

    def vod_playlist(
        self,
        track: str,
        segments: Optional[Sequence[SegmentRef]] = None,
        encode_uri: Optional[Callable[[str, str], str]] = None,
    ) -> str:
        """该轨的 VOD m3u8 文本（含 `#EXT-X-ENDLIST`）。

        **对外只出成品，不出骨架** —— 只出骨架等于要求每个调用方自己备料，而备料
        （EXTINF 真值、滤在途、判 init、算 TARGETDURATION）才是写错会静默的那部分：
        骨架写错播放器立刻报错，备料写错表现为 hls.js 段尾停摆、缓冲洞、导出时长错乱。

        Args:
            track: "raw" 或 "processed"
            segments: 要收进 playlist 的段；None 表示整轨可播段。传进来的会**再滤一道
                在途段**（调用方可能来自 `segments_around`，那里刻意不滤）。
            encode_uri: `(kind, filename) -> uri`，把段身份翻成 URI。`kind ∈
                {"segment", "init"}`。默认恒等 —— 裸文件名就是合法的相对 URI，在与
                init/段同目录的 m3u8 里能正确解析。要 token 化 URL 的从这里注入。

        Raises:
            StepInitMissing: 该轨缺 init 段
            StepNoPlayableSegments: 无可播段（无段，或全部在途）

        `#EXT-X-TARGETDURATION` 取 `round(max EXTINF)`：RFC 8216 §4.3.2.1 的判据就是
        「EXTINF 四舍五入后 MUST ≤ TARGETDURATION」，故 round 是正解而非 ceil。
        """
        self._check_track(track)
        if not self.has_init(track):
            raise StepInitMissing(
                f"{layout.init_name(track)} not found for task {self.task_id} "
                f"step {self.step_id}. This step is either mid-transcode or written "
                f"in an unsupported legacy layout."
            )

        durations = self._extinf(track)
        candidates = self.segments(track) if segments is None else list(segments)
        playable = [s for s in candidates if s.filename in durations]
        if not playable:
            raise StepNoPlayableSegments(
                f"No playable {track} segments for task {self.task_id} "
                f"step {self.step_id} (no segments, or all in-flight)"
            )

        uri = encode_uri if encode_uri is not None else (lambda _kind, name: name)
        seg_durs = [durations[s.filename] for s in playable]
        return playlist.build_vod_playlist(
            entries=[
                (uri("segment", s.filename), d) for s, d in zip(playable, seg_durs)
            ],
            map_uri=uri("init", layout.init_name(track)),
            target_duration=max(int(round(max(seg_durs))), 1),
        )

    def frames(
        self,
        track: str = "raw",
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        width: int = 640,
        height: int = 480,
    ) -> Iterator["Frame"]:
        """把 `[start_ts, end_ts]` 区间解码成像素帧（惰性流，ts 升序）。

        ⚠ **会起 ffmpeg 子进程**（每段一个）。成本写在返回类型里：`Iterator` 本身就
        说明它是惰性流，不必在名字上再写一遍（见命名规则）。

        缺 sidecar 的段**跳过、不抛** —— 宽容留在这层（缺一段的索引不该让前后所有段
        一起读不了），严格留在消费侧（`inference/offline/frame_finder.FrameFinder`
        按 ts 对号，配不上就 ValueError）。

        `start_ts` / `end_ts` 为 None 表示该侧不设限。
        """
        self._check_track(track)
        # 函数体内 import：segment_decoder 吃 numpy 且模块级回指本模块，顶层 import
        # 会让「只想拿个段清单」的调用方也付这笔钱。
        from app.services.step_store.segment_decoder import SegmentDecoder

        yield from SegmentDecoder.for_step(self, track).iter(
            start_ts, end_ts, width, height
        )

    # ---------------- 具名文件操作 ----------------

    def find_product(self, filename: str) -> Optional[Path]:
        """按**外部给的**文件名取该 step 下的产物；不存在或越界返回 None。

        **path traversal 防御在这里，不在调用方**：`filename` 来自外部（媒体 token 的
        payload），必须确保解析后仍落在存储根内。这段检查曾写在 `routers/media.py`，
        连带把「根在哪、目录怎么拼」也漏给了协议层。

        与 `product_path` 的分工见后者 docstring。
        """
        if "/" in filename or "\\" in filename or filename in (".", ".."):
            return None
        candidate = (self._dir / filename).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def product_path(self, kind: str, **key) -> Path:
        """该产物该写到哪。**顺带保证 step 目录存在**（写侧入口）。

        Args:
            kind: 必须在 `purge.PRODUCTS` 里登记，否则抛 `KeyError`。
            **key: 该 kind 的命名参数（如 segment 要 `track` + `ts_us`）。

        **登记是强制的**：未登记的产物对 TTL 不可见 —— 表现为「目录被提前回收，而那个
        产物还有人要读」。此前 `PRODUCTS` 只是文档性注册表，忘登记不报错；现在 kind
        校验把它变成运行时硬约束。

        与 `find_product` 的分工（同一资源的读写共用词根，前缀区分输入可信度）：
        本方法按**已登记的 kind** 算，必返回一个路径（文件可以还不存在）；
        `find_product` 按**外部文件名**找，可能 `None`。
        """
        name = purge.product_name(kind, **key)
        step_dir = self._dir
        step_dir.mkdir(parents=True, exist_ok=True)
        return step_dir / name

    def open_product(
        self, kind: str, mode: str = "r", *, encoding: Optional[str] = None, **key
    ) -> IO:
        """直接开产物的文件句柄，路径不出包。`kind` 校验同 `product_path`。

        写模式（`w`/`a`/`x`）经 `product_path` 保证 step 目录存在；**读模式不建目录**
        —— 读一个不存在的 step 不该在盘上留下痕迹（空目录没有任何已登记产物，
        `last_activity_at` 返回 None，TTL 按契约不删它，等于永久泄漏一个空目录）。
        文件不存在照常抛 `FileNotFoundError`，调用方按需 catch。

        `encoding` 默认 utf-8；二进制模式忽略它。
        """
        if any(c in mode for c in "wax"):
            path = self.product_path(kind, **key)
        else:
            path = self._dir / purge.product_name(kind, **key)
        if "b" in mode:
            return path.open(mode)
        return path.open(mode, encoding=encoding or "utf-8")

    def scratch_path(self, prefix: str, suffix: str = ".m3u8") -> Path:
        """step 目录内的临时文件路径（调用方负责建与删）。顺带保证目录存在。

        **必须落在 step 目录**是格式约束而非随意选择：临时 m3u8 里的 `EXT-X-MAP` 与段
        URI 都是相对引用，只有与 init/段同目录才解析得到。故路径由本包给，调用方不自己拼。

        **前导点命名**同样是约束：`purge` 靠它把临时文件排除在产物之外，否则崩溃残留
        会让死 step 躲过 TTL 回收（`.{stem}.tmp_init.mp4` 会命中 `*_init.mp4` 这类
        产物 glob，光靠 glob 区分不开）。新增临时文件必须沿用前导点。

        要 ffmpeg 的 `cwd` 取本方法返回值的 `.parent` —— 那是唯一正当的「拿到目录」
        的方式，见模块 docstring。
        """
        step_dir = self._dir
        step_dir.mkdir(parents=True, exist_ok=True)
        return step_dir / f".{prefix}_{secrets.token_hex(6)}{suffix}"


# ---------------------------------------------------------------------------
# 模块级函数 —— 取句柄、枚举、删除
#
# 这几个动作没有身份也没有状态：存储根一律自解析（单一真源），**任何进程级状态都不
# 得挂到本模块上** —— 目录锁留在各写者手里，理由见抽包记录 §5「并发：锁不进本包」。
# ---------------------------------------------------------------------------


def step(task_id: int, step_id: int) -> Step:
    """取该 step 的句柄。**目录不存在也返回** —— 句柄是路由键的载体，不是存在性
    断言；「有没有段」问 `step.tracks`，「有没有产物」问 `step.last_activity_at`。"""
    return Step(task_id, step_id)


def steps(task_id: Optional[int] = None, include_empty: bool = False) -> List[Step]:
    """列 step 句柄。

    Args:
        task_id: None 表示**全局枚举**（TTL 回收用）；给了则只列该 task 的。
        include_empty: 默认 False，丢弃「两轨都没段」的 step —— 目录建了但没写成段
            （起流即失败）对回放没有意义，清单不该把它露给前端点开黑屏。
            传 True 才看得见这类目录，而 **TTL 恰恰要看见**：只有 `features.jsonl`
            没有 HLS 段的 step 正是此前泄漏的那一类。

    两种枚举的语义差是刻意的，用一个显式开关承载比两个同名不同义的函数安全。
    返回按 (task_id, step_id) 升序。清单本就要扫盘才能过滤空 step，返回惰性
    `Step` 零额外成本。
    """
    base_dir = storage_root()

    if include_empty:
        pairs: Iterator[Tuple[int, int]] = purge.iter_steps(base_dir)
        if task_id is not None:
            pairs = (p for p in pairs if p[0] == task_id)
        return [step(t, s) for t, s in pairs]

    task_ids = tasks() if task_id is None else [task_id]
    found: List[Step] = []
    for tid in task_ids:
        task_root = base_dir / str(tid)
        if not task_root.is_dir():
            continue
        step_ids = sorted(
            sid
            for entry in task_root.iterdir()
            if entry.is_dir() and (sid := _dir_name_to_int(entry.name)) is not None
        )
        for sid in step_ids:
            handle = step(tid, sid)
            if handle.tracks:  # 扫一次，结果留在句柄缓存里，调用方不必再扫
                found.append(handle)
    return found


def tasks(recent_first: bool = False) -> List[int]:
    """列存储根下的 task id。只认数字目录名（`.lab_exports` 等跳过）。

    Args:
        recent_first: 默认 False = 升序。True = 按「最近有段落盘」倒序，排序键取
            `max(该 task 下各 step 目录的 mtime)`。段文件写入会更新其所在 step
            目录的 mtime，故该值 ≈ 最后一段落盘时刻。

    **`recent_first` 是廉价粗排，仅供挑深扫候选**：只 stat 目录、不进目录读段文件，
    成本 O(目录数) 而非 O(总段文件数)。近似性是有意的 —— mtime 只决定「先深扫谁」，
    绝不对外当时间戳用，对外时间一律取 `segments()` 的真实 ts_us。无 step 子目录的
    task 排序键取 0（排最后），但仍保留在结果里，由调用方深扫时丢弃。

    不校验目录内是否真有段（那要深扫，交给调用方按需 `steps(task_id)`）。
    """
    base_dir = storage_root()
    if not base_dir.is_dir():
        return []

    entries = [
        (entry, tid)
        for entry in base_dir.iterdir()
        if entry.is_dir() and (tid := _dir_name_to_int(entry.name)) is not None
    ]
    if not recent_first:
        return sorted(tid for _, tid in entries)

    keyed: List[Tuple[float, int]] = []
    for entry, task_id in entries:
        mtimes = [
            child.stat().st_mtime
            for child in entry.iterdir()
            if child.is_dir() and _dir_name_to_int(child.name) is not None
        ]
        keyed.append((max(mtimes) if mtimes else 0.0, task_id))
    keyed.sort(reverse=True)  # mtime 降序；同 mtime 时 task_id 大者优先
    return [task_id for _, task_id in keyed]


def purge_step(task_id: int, step_id: int) -> bool:
    """删除整个 step 目录，返回是否真的删了（不存在返回 False）。

    ⚠ **删的是整个目录含所有写者的产物**，不只调用方自己写的那些：HLS 段与
    playlist、`features.jsonl` / `facts.jsonl`、离线推理结果一并消失。见 `PRODUCTS`。

    ⚠ **只执行删除，不判断该不该删。** 保留策略在
    `persistence/workers/cleanup_worker.py`；重启 supersede 的判断在
    `persistence/strategies/hls_strategy.purge_step_dir`。

    ⚠ **调用方必须在本调用返回之后，才能创建本 run 的任何产物**（见
    `purge.purge_step` 的完整契约）。

    **刻意不挂在 `Step` 上**：它跨所有写者、没有单一归属；且作为自由函数意味着调用方
    必须重新写出两个 id，不能顺手 `step.purge()`。并发控制也不在此（写侧持自己的
    目录锁 —— 锁是并发机制，不随删除动作搬家）。
    """
    return purge.purge_step(layout.step_dir(storage_root(), task_id, step_id))


def sweep_empty_tasks() -> int:
    """回收 step 全被抽走后留下的空 task 目录，返回删除个数。"""
    return purge.sweep_empty_tasks(storage_root())


def storage_root() -> Path:
    """存储根目录（与 hls_strategy 写入路径同源）。

    直读 `settings.storage_base_dir` 单一真源，保证读写两侧对相对路径的解析逻辑
    完全一致（相对路径都以项目根为基）。

    **`app/` 内除本包外没有调用方**，且由 `test_import_hygiene` 的门禁锁死 ——
    根拿不到，路径就无从拼起。包外只有 `integration_tests` 造数据时用。

    settings 是 L3（读环境、有副作用），故 import 写在函数体内；也正因为它是**函数**
    而非模块级常量，测试 monkeypatch `settings.storage_dir` 后本函数立即返回新值。
    """
    from app.settings import settings

    return settings.storage_base_dir
