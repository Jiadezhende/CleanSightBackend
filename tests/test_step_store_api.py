"""
step_store 对外接口单元测试（模块级函数 / `Step` 句柄 / `SegmentRef` 值对象）。

覆盖：
- segments：按 ts_us 升序返回、过滤非匹配文件、playable_only 两种语义
- steps / tasks：清单接口的落盘枚举，含 include_empty / recent_first 两个开关
- vod_playlist：备料（滤在途、EXTINF 真值、TARGETDURATION）与两个领域异常
- find_product：path traversal 防御
- 写成员（`segment_path` / `open_features` / …）：落盘名、建目录、track 校验
- 活动标记：写成员刷、读入口与 scratch_path 不刷（TTL 判据）
- 边界：ts_ms 早于第一段、晚于最后一段、空目录、track 非法、路径不存在

落盘约定：{base_dir}/{task_id}/{step_id}/

存储根一律经 `tmp_storage` fixture 指到临时目录（monkeypatch `settings.storage_dir`）——
本包不再收 base_dir 参数，根是单一真源。
"""

import os
from pathlib import Path

import pytest

from app.services.step_store import store as step_store
from app.services.step_store.store import (
    SegmentRef,
    StepInitMissing,
    StepNoPlayableSegments,
)


def _make_step_dir(base: Path, task_id: int, step_id: int) -> Path:
    d = base / str(task_id) / str(step_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _touch_segment(step_dir: Path, track: str, ts_us: int) -> None:
    (step_dir / f"{track}_segment_{ts_us}.mp4").write_bytes(b"")


def _write_playlist(step_dir: Path, track: str, ts_us_list, dur: float = 10.0) -> None:
    """写 LIVE 形态 playlist（不含 ENDLIST），只收录 ts_us_list 里的段。

    不在其中的段即「在途段」—— 磁盘上有 mp4 但 transcode+append 未完成。
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", f'#EXT-X-MAP:URI="{track}_init.mp4"']
    for ts in ts_us_list:
        lines += [f"#EXTINF:{dur:.3f},", f"{track}_segment_{ts}.mp4"]
    (step_dir / f"{track}_playlist.m3u8").write_text("\n".join(lines) + "\n")


class TestSegments:
    def test_returns_empty_when_dir_missing(self, tmp_storage):
        step = step_store.step(1, 1)
        assert step.segments("raw", playable_only=False) == []
        assert step.segments("processed", playable_only=False) == []

    def test_lists_segments_sorted_ascending(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 100, 1)
        for ts in (3000, 1000, 2000):
            _touch_segment(d, "processed", ts)

        segs = step_store.step(100, 1).segments("processed", playable_only=False)
        assert [s.ts_us for s in segs] == [1000, 2000, 3000]

    def test_filters_by_track(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 2)
        _touch_segment(d, "raw", 100)
        _touch_segment(d, "raw", 200)
        _touch_segment(d, "processed", 100)
        step = step_store.step(1, 2)

        assert {s.ts_us for s in step.segments("raw", playable_only=False)} == {100, 200}
        assert {s.ts_us for s in step.segments("processed", playable_only=False)} == {100}

    def test_step_id_isolation(self, tmp_storage):
        """同一 task 的不同 step 互不干扰。"""
        _touch_segment(_make_step_dir(tmp_storage, 7, 1), "processed", 1000)
        _touch_segment(_make_step_dir(tmp_storage, 7, 2), "processed", 2000)
        assert [s.ts_us for s in step_store.step(7, 1).segments("processed", playable_only=False)] == [1000]
        assert [s.ts_us for s in step_store.step(7, 2).segments("processed", playable_only=False)] == [2000]

    def test_ignores_non_matching_files(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        _touch_segment(d, "processed", 100)
        (d / "metadata.json").write_text("{}")
        (d / "raw_playlist.m3u8").write_text("")
        (d / "stray.json").write_text("[]")
        (d / "garbage.mp4").write_bytes(b"")

        segs = step_store.step(1, 1).segments("processed", playable_only=False)
        assert [s.filename for s in segs] == ["processed_segment_100.mp4"]

    def test_invalid_track_raises(self, tmp_storage):
        with pytest.raises(ValueError, match="Invalid track"):
            step_store.step(1, 1).segments("bogus")


class TestPlayableOnly:
    """在途段 = mp4 已落盘但不在 playlist 里（transcode+append 未完成）。"""

    def test_default_filters_in_flight_segments(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        _touch_segment(d, "raw", 1000)
        _touch_segment(d, "raw", 2000)
        _write_playlist(d, "raw", [1000])  # 2000 仍在途

        step = step_store.step(1, 1)
        assert [s.ts_us for s in step.segments("raw")] == [1000]
        assert [s.ts_us for s in step.segments("raw", playable_only=False)] == [1000, 2000]

    def test_no_playlist_means_nothing_playable(self, tmp_storage):
        """整个 playlist 缺失（历史遗留 / 首段仍在 transcode）→ 无可播段。"""
        d = _make_step_dir(tmp_storage, 1, 1)
        _touch_segment(d, "raw", 1000)

        step = step_store.step(1, 1)
        assert step.segments("raw") == []
        assert len(step.segments("raw", playable_only=False)) == 1


class TestTimeBounds:
    def test_none_without_playlist(self, tmp_storage):
        _touch_segment(_make_step_dir(tmp_storage, 7, 1), "raw", 1_000_000)
        assert step_store.step(7, 1).time_bounds_us is None

    def test_end_includes_last_segment_extinf(self, tmp_storage):
        """终点取 max(ts + EXTINF)，不是 max(ts) —— 后者漏掉最后一段自身长度。"""
        d = _make_step_dir(tmp_storage, 7, 1)
        _touch_segment(d, "raw", 1_000_000)
        _touch_segment(d, "raw", 11_000_000)
        _write_playlist(d, "raw", [1_000_000, 11_000_000], dur=10.0)

        assert step_store.step(7, 1).time_bounds_us == (1_000_000, 21_000_000)

    def test_spans_both_tracks(self, tmp_storage):
        """双轨取并集：两轨段边界不一定对齐。"""
        d = _make_step_dir(tmp_storage, 7, 1)
        _touch_segment(d, "raw", 1_000_000)
        _touch_segment(d, "processed", 5_000_000)
        _write_playlist(d, "raw", [1_000_000], dur=2.0)
        _write_playlist(d, "processed", [5_000_000], dur=2.0)

        assert step_store.step(7, 1).time_bounds_us == (1_000_000, 7_000_000)

    def test_skips_in_flight_segments(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 7, 1)
        _touch_segment(d, "raw", 1_000_000)
        _touch_segment(d, "raw", 99_000_000)  # 在途，无 EXTINF
        _write_playlist(d, "raw", [1_000_000], dur=3.0)

        assert step_store.step(7, 1).time_bounds_us == (1_000_000, 4_000_000)


class TestVodPlaylist:
    @pytest.fixture
    def step_dir(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        (d / "raw_init.mp4").write_bytes(b"init")
        for ts in (1_000_000, 11_000_000):
            _touch_segment(d, "raw", ts)
        _write_playlist(d, "raw", [1_000_000, 11_000_000], dur=10.4)
        return d

    def test_full_text(self, step_dir, tmp_storage):
        text = step_store.step(1, 1).vod_playlist("raw")
        assert text.splitlines() == [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            # RFC 8216 §4.3.2.1：判据是「EXTINF 四舍五入后 ≤ TARGETDURATION」，
            # 故 10.4 → 10（round），不是 ceil 的 11
            "#EXT-X-TARGETDURATION:10",
            "#EXT-X-MEDIA-SEQUENCE:0",
            '#EXT-X-MAP:URI="raw_init.mp4"',
            "#EXTINF:10.400,",
            "raw_segment_1000000.mp4",
            "#EXTINF:10.400,",
            "raw_segment_11000000.mp4",
            # 缺 ENDLIST → 播放端当直播流只读 live edge，前面的段全丢
            "#EXT-X-ENDLIST",
        ]

    def test_encode_uri_injects_token_form(self, step_dir, tmp_storage):
        text = step_store.step(1, 1).vod_playlist(
            "raw", encode_uri=lambda kind, name: f"https://x/{kind}/{name}"
        )
        assert '#EXT-X-MAP:URI="https://x/init/raw_init.mp4"' in text
        assert "https://x/segment/raw_segment_1000000.mp4" in text

    def test_filters_in_flight_from_explicit_segments(self, step_dir, tmp_storage):
        """调用方给的段（如取证上下文）也要再滤一道 —— 那边刻意不滤。"""
        _touch_segment(step_dir, "raw", 21_000_000)  # 在途
        step = step_store.step(1, 1)
        text = step.vod_playlist("raw", segments=step.segments("raw", playable_only=False))
        assert "raw_segment_21000000.mp4" not in text

    def test_missing_init_raises(self, step_dir, tmp_storage):
        (step_dir / "raw_init.mp4").unlink()
        with pytest.raises(StepInitMissing, match="raw_init.mp4"):
            step_store.step(1, 1).vod_playlist("raw")

    def test_all_in_flight_raises(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        (d / "raw_init.mp4").write_bytes(b"init")
        _touch_segment(d, "raw", 1_000_000)
        with pytest.raises(StepNoPlayableSegments):
            step_store.step(1, 1).vod_playlist("raw")

    def test_target_duration_floor_is_one(self, tmp_storage):
        """亚秒段（退化段）不能声明 TARGETDURATION:0。"""
        d = _make_step_dir(tmp_storage, 1, 1)
        (d / "raw_init.mp4").write_bytes(b"init")
        _touch_segment(d, "raw", 1_000)
        _write_playlist(d, "raw", [1_000], dur=0.2)
        assert "#EXT-X-TARGETDURATION:1" in step_store.step(1, 1).vod_playlist("raw")


class TestSteps:
    """steps()：清单接口的落盘枚举。"""

    def test_returns_empty_when_task_dir_missing(self, tmp_storage):
        assert step_store.steps(999) == []

    def test_reports_tracks_actually_on_disk(self, tmp_storage):
        # step 1 双轨，step 2 只有 raw —— 后者是大屏按 track 默认 processed 打 404 的成因
        d1 = _make_step_dir(tmp_storage, 7, 1)
        _touch_segment(d1, "raw", 1000)
        _touch_segment(d1, "processed", 1000)
        _touch_segment(_make_step_dir(tmp_storage, 7, 2), "raw", 5000)

        steps = step_store.steps(7)
        assert [s.step_id for s in steps] == [1, 2]
        assert steps[0].tracks == ("raw", "processed")
        assert steps[1].tracks == ("raw",)

    def test_drops_step_dir_without_segments(self, tmp_storage):
        _make_step_dir(tmp_storage, 7, 1)  # 目录建了但没段（起流即失败）
        _touch_segment(_make_step_dir(tmp_storage, 7, 2), "raw", 1000)

        assert [s.step_id for s in step_store.steps(7)] == [2]

    def test_include_empty_sees_segmentless_dirs(self, tmp_storage):
        """**TTL 的命门**：只有 features.jsonl 没有 HLS 段的目录正是此前泄漏的那一类。"""
        (_make_step_dir(tmp_storage, 7, 1) / "features.jsonl").write_text("{}")
        _touch_segment(_make_step_dir(tmp_storage, 7, 2), "raw", 1000)

        assert [s.step_id for s in step_store.steps(7)] == [2]
        assert [s.step_id for s in step_store.steps(7, include_empty=True)] == [1, 2]

    def test_skips_non_numeric_step_dirs(self, tmp_storage):
        (tmp_storage / "7" / "scratch").mkdir(parents=True)
        _touch_segment(_make_step_dir(tmp_storage, 7, 1), "raw", 1000)

        assert [s.step_id for s in step_store.steps(7)] == [1]

    def test_global_enumeration_spans_tasks(self, tmp_storage):
        _touch_segment(_make_step_dir(tmp_storage, 7, 1), "raw", 1000)
        _touch_segment(_make_step_dir(tmp_storage, 9, 3), "raw", 1000)

        got = step_store.steps()
        assert [(s.task_id, s.step_id) for s in got] == [(7, 1), (9, 3)]


class TestTasks:
    def test_returns_empty_when_base_dir_missing(self, tmp_storage, monkeypatch):
        from app.settings import settings

        monkeypatch.setattr(settings, "storage_dir", str(tmp_storage / "nope"))
        assert step_store.tasks() == []

    def test_skips_non_numeric_dirs_and_sorts(self, tmp_storage):
        for task_id in (30, 10, 20):
            _make_step_dir(tmp_storage, task_id, 1)
        (tmp_storage / ".lab_exports").mkdir()
        (tmp_storage / "README.md").write_text("x")

        assert step_store.tasks() == [10, 20, 30]

    def test_recent_first_orders_by_newest_step_dir_mtime(self, tmp_storage):
        for task_id, mtime in ((1, 1_000), (2, 3_000), (3, 2_000)):
            d = _make_step_dir(tmp_storage, task_id, 1)
            _touch_segment(d, "raw", 100)
            os.utime(d, (mtime, mtime))  # 确定的 mtime，不靠写入顺序

        assert step_store.tasks(recent_first=True) == [2, 3, 1]

    def test_recent_first_uses_max_step_mtime_within_a_task(self, tmp_storage):
        os.utime(_make_step_dir(tmp_storage, 1, 1), (1_000, 1_000))
        os.utime(_make_step_dir(tmp_storage, 1, 2), (9_000, 9_000))
        os.utime(_make_step_dir(tmp_storage, 2, 1), (5_000, 5_000))

        # task 1 取 max(1000, 9000)=9000 → 排在 task 2 前面
        assert step_store.tasks(recent_first=True) == [1, 2]

    def test_task_without_step_dirs_sorts_last_but_is_kept(self, tmp_storage):
        os.utime(_make_step_dir(tmp_storage, 1, 1), (1_000, 1_000))
        (tmp_storage / "2").mkdir()  # 空 task 目录：排序键 0，仍保留由调用方深扫丢弃

        assert step_store.tasks(recent_first=True) == [1, 2]


class TestFindProduct:
    """按外部给的文件名取产物 —— path traversal 防御在包内，不在协议层。"""

    def test_returns_path_for_existing_file(self, tmp_storage):
        d = _make_step_dir(tmp_storage, 1, 1)
        _touch_segment(d, "raw", 100)
        got = step_store.step(1, 1).find_product("raw_segment_100.mp4")
        assert got == d / "raw_segment_100.mp4"

    def test_none_for_missing_file(self, tmp_storage):
        _make_step_dir(tmp_storage, 1, 1)
        assert step_store.step(1, 1).find_product("nope.mp4") is None

    @pytest.mark.parametrize(
        "evil", ["../../etc/passwd", "..\\..\\secret", "sub/dir.mp4", "..", "."]
    )
    def test_rejects_traversal(self, tmp_storage, evil):
        _make_step_dir(tmp_storage, 1, 1)
        assert step_store.step(1, 1).find_product(evil) is None


class TestWriteMembers:
    """每类产物一个具名成员：签名即命名参数，落盘名与 layout 单一真源逐字一致。"""

    def test_named_members_land_in_step_dir(self, tmp_storage):
        step = step_store.step(1, 1)
        assert step.segment_path("raw", 42).name == "raw_segment_42.mp4"
        assert step.sidecar_path("raw", 42).name == "raw_segment_42.idx"
        assert step.init_path("processed").name == "processed_init.mp4"
        assert step.playlist_path("raw").name == "raw_playlist.m3u8"
        assert step.metadata_path().name == "metadata.json"
        assert step.features_path().name == "features.jsonl"
        assert step.facts_path().name == "facts.jsonl"
        assert step.segment_path("raw", 42).parent == tmp_storage / "1" / "1"

    def test_creates_step_dir(self, tmp_storage):
        step_store.step(1, 1).metadata_path()
        assert (tmp_storage / "1" / "1").is_dir()

    def test_rejects_invalid_track(self, tmp_storage):
        with pytest.raises(ValueError, match="Invalid track"):
            step_store.step(1, 1).segment_path("bogus", 42)

    def test_open_roundtrip(self, tmp_storage):
        step = step_store.step(1, 1)
        with step.open_features("a") as f:
            f.write('{"ts": 1}\n')
        with step.open_features() as f:
            assert f.read() == '{"ts": 1}\n'

    def test_open_read_missing_raises(self, tmp_storage):
        with pytest.raises(FileNotFoundError):
            step_store.step(1, 1).open_facts()


class TestActivityMarker:
    """TTL 的唯一判据：每个写入口顺带刷新它，读入口一律不碰。"""

    def test_every_write_member_stamps_activity(self, tmp_storage):
        """新增写成员漏调 `_write_dir()`，只表现为该写者独占的 step 静默过期被回收。"""
        writes = [
            lambda s: s.segment_path("raw", 1),
            lambda s: s.sidecar_path("raw", 1),
            lambda s: s.init_path("raw"),
            lambda s: s.playlist_path("raw"),
            lambda s: s.metadata_path(),
            lambda s: s.features_path(),
            lambda s: s.facts_path(),
            lambda s: s.open_features("w").close(),
            lambda s: s.open_facts("w").close(),
            lambda s: s.open_offline_result("w").close(),
        ]
        for i, write in enumerate(writes):
            step = step_store.step(2, i)
            assert step.last_activity_at is None
            write(step)
            assert step.last_activity_at is not None, write

    def test_read_open_does_not_stamp_or_create_dir(self, tmp_storage):
        """读一个不存在的 step 不该在盘上留痕，也不该让它显得还活着。"""
        step = step_store.step(1, 1)
        with pytest.raises(FileNotFoundError):
            step.open_features("r")
        assert not (tmp_storage / "1" / "1").exists()
        assert step.last_activity_at is None

    def test_scratch_path_does_not_stamp(self, tmp_storage):
        """临时文件是读侧导出/打点的中间产物，不代表这个 step 还在产出。"""
        step = step_store.step(1, 1)
        step.scratch_path("export")
        assert step.last_activity_at is None

    def test_marker_is_on_disk_not_in_the_handle(self, tmp_storage):
        step_store.step(1, 1).metadata_path()
        assert step_store.step(1, 1).last_activity_at is not None


class TestScratchPath:
    def test_leading_dot_keeps_temp_files_out_of_segment_scan(self, tmp_storage):
        """前导点让半截的临时 mp4 落在段名正则之外，不被 `segments()` 当成真段。"""
        p = step_store.step(1, 1).scratch_path("clip")
        assert p.name.startswith(".clip_")
        assert p.suffix == ".m3u8"
        assert p.parent == tmp_storage / "1" / "1"

    def test_suffix_is_overridable(self, tmp_storage):
        p = step_store.step(1, 1).scratch_path("facts", suffix=".jsonl")
        assert p.name.startswith(".facts_") and p.suffix == ".jsonl"

    def test_unique_per_call(self, tmp_storage):
        step = step_store.step(1, 1)
        assert step.scratch_path("clip") != step.scratch_path("clip")


class TestSegmentRef:
    def test_ts_conversions(self):
        ref = SegmentRef(filename="raw_segment_1234567.mp4", ts_us=1_234_567)
        assert ref.ts_ms == 1234
        assert abs(ref.ts_s - 1.234567) < 1e-9

    def test_carries_only_what_the_caller_cannot_get_elsewhere(self):
        """task_id/step_id/track/path 都从句柄或构造参数可得，故不在值对象上。"""
        assert SegmentRef._fields == ("filename", "ts_us")


class TestBaseDirResolution:
    """存储根目录由 settings.storage_base_dir 单一真源解析，与进程 cwd 无关。

    「读写两侧同源」此前靠断言 `PersistenceConfig.storage_base_dir == _storage_root()`
    来守；现在它是**结构性保证**——除 settings 与本包外没人能读到那个属性（由
    `test_import_hygiene.test_storage_root_is_private_to_step_store` 锁死），
    persistence 侧的那个转发 property 已随之删除。
    """

    def test_relative_base_dir_resolves_to_project_root_regardless_of_cwd(
        self, tmp_path, monkeypatch
    ):
        from app.settings import settings
        from app.services.step_store.store import _storage_root

        monkeypatch.setattr(settings, "storage_dir", "./database")
        write_path = settings.storage_base_dir
        assert write_path.is_absolute()
        assert write_path.name == "database"

        # 切到完全无关的 cwd，解析的绝对路径必须保持不变（以项目根为基，非 cwd）
        monkeypatch.chdir(tmp_path)
        assert settings.storage_base_dir == write_path
        assert tmp_path not in write_path.parents

        # 本包的默认根解析与 settings 同值（读写两侧共用这一个入口）
        assert _storage_root() == write_path
        assert step_store.step(1, 1)._root == write_path

    def test_absolute_base_dir_is_returned_as_is(self, tmp_path, monkeypatch):
        from app.settings import settings

        abs_dir = tmp_path / "custom" / "store"
        monkeypatch.setattr(settings, "storage_dir", str(abs_dir))
        assert settings.storage_base_dir == abs_dir.resolve()
