"""`(task_id, step_id)` → 这个 step 的一切。本包的对外门面（包契约见 `__init__.py`）。

    Step        绑定一次路由键的句柄，读写的成组能力都挂在它上面
    SegmentRef  一个段的不可变值对象（filename + ts_us）
    模块级函数   step / steps / tasks / purge_step / sweep_empty_tasks —— 无身份无状态，
                存储根一律自解析

    from app.services.step_store import store as step_store
    step = step_store.step(task_id, step_id)

**函数一律模块限定，绝不裸导入**：`step` / `steps` / `tasks` 在调用方是高频局部变量名，
`steps = steps(task_id)` 就地遮蔽同名全局，同一函数里再调一次直接 `UnboundLocalError`。

**不提供任何返回目录的成员**（理由见包 docstring）。要 ffmpeg 的 `cwd` 取
`scratch_path().parent`。命名真源在 [layout.py](layout.py)，本模块不自建。
"""

from __future__ import annotations

import logging
import secrets
import shutil
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

from app.services.step_store import layout, playlist, products

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

    只有两种可能：旧格式产物（无迁移路径），或首段仍在 transcode（窗口极短）。**服务端
    都无法自愈**，故调用方按「此 step 不可播放/导出」处理。
    """


class StepNoPlayableSegments(StepStoreError):
    """该轨没有已完成落盘的段（无段，或全部在途）。"""


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------


class SegmentRef(NamedTuple):
    """单个 HLS 段的引用。**只带调用方拿不到的东西** —— 刻意不带 task_id / step_id /
    track / path / is_trigger / duration，逐项理由见抽包记录 §2.3。
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


# ---------------------------------------------------------------------------
# Step —— 绑定一次路由键的句柄
# ---------------------------------------------------------------------------


class Step:
    """一个 `(task_id, step_id)` 落盘单元的句柄。

    目录扫描与逐轨 playlist 解析首次访问时各做一遍，之后在**本对象生命周期内**缓存 ——
    读侧是成组用这些能力的（traceback 一个请求就要段列表 + EXTINF + init 判据 + 时间跨度）。

    ⚠ **不得跨请求持有**：活跃 step 每 ~10s 落一个新段，缓存会过期。每次请求/每轮扫描重新
    `step_store.step(...)`，构造零成本（不碰盘、不读 settings）。

    命名规则（抽包记录 §9）：property = 零成本或已随目录扫描缓存，method = 至少一次 I/O；
    动词只留给变盘的成员，`has_`/`is_`/`find_` 不受此限。
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
        """存储根。**首次碰盘时才解析并缓存** —— `steps()` 一次可构造上百个句柄，构造期
        解析等于每个句柄都走一遍 settings + `Path.resolve()`。"""
        if self._base_dir is None:
            self._base_dir = storage_root()
        return self._base_dir

    @property
    def _dir(self) -> Path:
        """step 目录。**包内私有**，绝不出包。"""
        return self._root / layout.step_subpath(self.task_id, self.step_id)

    def _scan(self) -> Dict[str, List[SegmentRef]]:
        """单次 iterdir 扫出按轨分组的段（各轨内按 ts_us 升序），双轨只付一次目录遍历。
        目录不存在时返回各轨空列表。"""
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

        EXTINF 是段时长**唯一真值**（不能用文件名 ts 差重推）；键集合同时是「已完成
        transcode+append」的判据 —— 不在其中的是在途段。
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

        **不能假定双轨**：大屏默认按 processed 打 playlist，只落了 raw 的 step 会 404。
        """
        by_track = self._scan()
        return tuple(t for t in layout.VALID_TRACKS if by_track[t])

    @property
    def time_bounds_us(self) -> Optional[Tuple[int, int]]:
        """双轨并集的 (最早段起点, 最晚段终点)，微秒；无可用段时 None。在途段查不到
        EXTINF，跳过。

        **终点取 max(seg.ts + EXTINF) 而非 max(seg.ts)** —— 后者漏掉最后一段自身长度。
        取并集是因为两轨段边界不一定对齐（实测有过 20+ 秒差），故它表达「该 step 有画面的
        时间跨度」，不等于任一单轨的播放范围。
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
        """最后一次产出的时刻（unix 秒）；无任何已登记产物时 None。判据见
        `products.last_activity`。

        供 TTL 判断「这目录还活着吗」，**「该不该删」不在本包**，在 `cleanup_worker.py`。
        """
        return products.last_activity(self._dir)

    def segments(self, track: str, playable_only: bool = True) -> List[SegmentRef]:
        """该轨全部段，ts 升序。

        Args:
            track: "raw" 或 "processed"
            playable_only: **默认 True，滤掉在途段**（mp4 已落盘故扫得到，但 transcode
                +append 未完成故不在 playlist 里）。放它们过去，fragment 实际媒体时长会与
                playlist 声明对不上：回放侧是 hls.js MSE 缓冲洞，导出侧是时长错乱。传
                False = 「磁盘上有什么就报什么」，供只报有没有画面、不解码也不拼 m3u8 的
                清单类接口用。

        Raises:
            ValueError: track 非法
        """
        self._check_track(track)
        segs = self._scan()[track]
        if not playable_only:
            return list(segs)
        durations = self._extinf(track)
        return [s for s in segs if s.filename in durations]

    def has_init(self, track: str) -> bool:
        """该轨的 fMP4 init 段是否已就位。缺了意味着什么见 `StepInitMissing`。"""
        self._check_track(track)
        return (self._dir / layout.init_name(track)).exists()

    def vod_playlist(
        self,
        track: str,
        segments: Optional[Sequence[SegmentRef]] = None,
        encode_uri: Optional[Callable[[str, str], str]] = None,
    ) -> str:
        """该轨的 VOD m3u8 文本（含 `#EXT-X-ENDLIST`）。

        Args:
            track: "raw" 或 "processed"
            segments: 要收进 playlist 的段；None 表示整轨可播段（**当前唯一在用的取值**）。
                显式传入时会**再滤一道在途段** —— 调用方若来自不滤在途段的查询，playlist
                声明的时长会与 fragment 实际媒体时长对不上，表现为 hls.js 缓冲洞。
            encode_uri: `(kind, filename) -> uri`，`kind ∈ {"segment", "init"}`。默认恒等
                （裸文件名在与 init/段同目录的 m3u8 里是合法相对 URI）。要 token 化 URL 的
                从这里注入。

        Raises:
            StepInitMissing: 该轨缺 init 段
            StepNoPlayableSegments: 无可播段（无段，或全部在途）

        `#EXT-X-TARGETDURATION` 取 `round(max EXTINF)` 而非 ceil：RFC 8216 §4.3.2.1 的判据
        就是「EXTINF 四舍五入后 MUST ≤ TARGETDURATION」。
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
        """把 `[start_ts, end_ts]` 区间解码成像素帧（惰性流，ts 升序）。None 表示该侧
        不设限。⚠ **会起 ffmpeg 子进程**，每段一个。

        缺 sidecar 的段**跳过、不抛** —— 宽容留在这层（缺一段的索引不该让前后所有段一起读
        不了），严格留在消费侧（`inference/offline/frame_finder.FrameFinder` 按 ts 对号，
        配不上就 ValueError）。
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
        payload），本方法保证解析后仍落在存储根内。
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
            kind: 必须在 `products.PRODUCTS` 里登记，否则抛 `KeyError` —— 未登记的产物对
                TTL 不可见，表现为「目录被提前回收，而那个产物还有人要读」。
            **key: 该 kind 的命名参数（如 segment 要 `track` + `ts_us`）。

        与 `find_product` 的分工：本方法按**已登记的 kind** 算，必返回一个路径（文件可以还
        不存在）；`find_product` 按**外部文件名**找，可能 `None`。
        """
        name = products.product_name(kind, **key)
        step_dir = self._dir
        step_dir.mkdir(parents=True, exist_ok=True)
        return step_dir / name

    def open_product(
        self, kind: str, mode: str = "r", *, encoding: Optional[str] = None, **key
    ) -> IO:
        """直接开产物的文件句柄，路径不出包。`kind` 校验同 `product_path`。

        写模式（`w`/`a`/`x`）经 `product_path` 保证 step 目录存在；**读模式不建目录** ——
        空目录没有任何已登记产物，`last_activity_at` 返回 None、TTL 按契约不删它，等于读一
        次不存在的 step 就永久泄漏一个空目录。文件不存在照常抛 `FileNotFoundError`。

        `encoding` 默认 utf-8；二进制模式忽略它。
        """
        if any(c in mode for c in "wax"):
            path = self.product_path(kind, **key)
        else:
            path = self._dir / products.product_name(kind, **key)
        if "b" in mode:
            return path.open(mode)
        return path.open(mode, encoding=encoding or "utf-8")

    def scratch_path(self, prefix: str, suffix: str = ".m3u8") -> Path:
        """step 目录内的临时文件路径（调用方负责建与删）。顺带保证目录存在。要 ffmpeg 的
        `cwd` 取返回值的 `.parent`。

        两条都是格式约束，不是风格：

        - **必须落在 step 目录** —— 临时 m3u8 里的 `EXT-X-MAP` 与段 URI 都是相对引用，只有
          与 init/段同目录才解析得到。
        - **前导点命名** —— `products` 靠它把临时文件排除在产物之外（`.{stem}.tmp_init.mp4`
          会命中 `*_init.mp4` 这类 glob），否则崩溃残留会让死 step 躲过 TTL 回收。新增临时
          文件必须沿用。
        """
        step_dir = self._dir
        step_dir.mkdir(parents=True, exist_ok=True)
        return step_dir / f".{prefix}_{secrets.token_hex(6)}{suffix}"


# ---------------------------------------------------------------------------
# 模块级函数 —— 取句柄、枚举、删除
#
# 无身份也无状态：存储根一律自解析。**任何进程级状态都不得挂到本模块上** —— 目录锁留在
# 各写者手里，理由见抽包记录 §5「并发：锁不进本包」。
# ---------------------------------------------------------------------------


def step(task_id: int, step_id: int) -> Step:
    """取该 step 的句柄。**目录不存在也返回** —— 句柄是路由键的载体，不是存在性断言。
    「有没有段」问 `step.tracks`，「有没有产物」问 `step.last_activity_at`。"""
    return Step(task_id, step_id)


def steps(task_id: Optional[int] = None, include_empty: bool = False) -> List[Step]:
    """列 step 句柄，按 (task_id, step_id) 升序。

    Args:
        task_id: None 表示**全局枚举**（TTL 回收用）；给了则只列该 task 的。
        include_empty: 默认 False，丢弃「两轨都没段」的 step —— 起流即失败留下的空目录对
            回放没意义，清单不该把它露给前端点开黑屏。**TTL 恰恰要看见这类目录**（只有
            `features.jsonl` 没有 HLS 段的 step 就属于它），故传 True。
    """
    base_dir = storage_root()

    if include_empty:
        pairs: Iterator[Tuple[int, int]] = products.iter_steps(base_dir)
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
            `max(该 task 下各 step 目录的 mtime)`。

    **`recent_first` 是廉价粗排，仅供挑深扫候选**：只 stat 目录不进目录，成本 O(目录数) 而
    非 O(总段文件数)。近似是有意的 —— mtime 只决定「先深扫谁」，**绝不对外当时间戳用**，对
    外时间一律取 `segments()` 的真实 ts_us。无 step 子目录的 task 排序键取 0，但仍在结果里。
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
    """删除整个 step 目录，返回是否真的删了（不存在返回 False）。best-effort：失败只打
    warning 并返回 False，不抛。

    ⚠ **删的是整个目录含所有写者的产物**：HLS 段与 playlist、`features.jsonl` /
    `facts.jsonl`、离线推理结果一并消失（见 `products.PRODUCTS`）。

    ⚠ **只执行删除，不判断该不该删** —— 保留策略在 `persistence/workers/cleanup_worker.py`，
    重启 supersede 的判断在 `persistence/strategies/hls_strategy.purge_step_dir`。

    ⚠ **调用方必须在本调用返回之后，才能创建本 run 的任何产物**：反过来，新建的
    `features.jsonl` 会被这里的 rmtree 抹掉且不报错。调用序见 `run_control.start_run`。
    """
    target = storage_root() / layout.step_subpath(task_id, step_id)
    if not target.exists():
        return False
    try:
        shutil.rmtree(target)
        return True
    except OSError as e:
        logger.warning("[StepStore] 删除 step 目录失败 %s: %s", target, e)
        return False


def sweep_empty_tasks() -> int:
    """回收 step 全被抽走后留下的空 task 目录，返回删除个数。

    `rmdir` 对非空目录会安全失败，故无需先检查是否为空。
    """
    root = storage_root()
    if not root.is_dir():
        return 0
    removed = 0
    for task_dir in sorted(root.iterdir()):
        if not task_dir.is_dir() or not task_dir.name.isdigit():
            continue
        try:
            task_dir.rmdir()
            removed += 1
            logger.info("[StepStore] 回收空 task 目录: %s", task_dir)
        except OSError:
            pass  # 非空或无权限，跳过
    return removed


def storage_root() -> Path:
    """存储根目录，直读 `settings.storage_base_dir` 单一真源。

    **`app/` 内除本包外没有调用方**，由 `test_import_hygiene` 门禁锁死 —— 根拿不到，路径就
    无从拼起。settings 是 L3 故 import 写在函数体内；也正因为它是**函数**而非模块级常量，
    测试 monkeypatch 后立即生效。
    """
    from app.settings import settings

    return settings.storage_base_dir
