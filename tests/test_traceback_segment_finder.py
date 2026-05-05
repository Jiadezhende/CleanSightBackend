"""
SegmentFinder 单元测试

覆盖：
- list_segments：按 ts_us 升序返回，过滤非匹配文件
- find：二分定位 + 上下文扩展
- 边界：ts_ms 早于第一段、晚于最后一段、空目录、track 非法
- 路径不存在 → 返回空列表
"""

from pathlib import Path

import pytest

from app.services.traceback.segment_finder import SegmentFinder, SegmentRef


def _make_task_dir(base: Path, client_id: str, task_id: int) -> Path:
    d = base / client_id / str(task_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _touch_segment(task_dir: Path, track: str, ts_us: int) -> None:
    (task_dir / f"{track}_segment_{ts_us}.mp4").write_bytes(b"")


def _touch_keypoints(task_dir: Path, ts_us: int) -> None:
    (task_dir / f"keypoints_{ts_us}.json").write_text("[]")


class TestListSegments:
    def test_returns_empty_when_dir_missing(self, tmp_path):
        finder = SegmentFinder(tmp_path)
        assert finder.list_segments("c", 1, "raw") == []
        assert finder.list_segments("c", 1, "processed") == []

    def test_lists_segments_sorted_ascending(self, tmp_path):
        d = _make_task_dir(tmp_path, "192.168.1.1", 100)
        _touch_segment(d, "processed", 3000)
        _touch_segment(d, "processed", 1000)
        _touch_segment(d, "processed", 2000)
        finder = SegmentFinder(tmp_path)

        segs = finder.list_segments("192.168.1.1", 100, "processed")
        assert [s.ts_us for s in segs] == [1000, 2000, 3000]
        assert all(s.track == "processed" for s in segs)
        assert segs[0].keypoints_filename == "keypoints_1000.json"

    def test_filters_by_track(self, tmp_path):
        d = _make_task_dir(tmp_path, "c", 1)
        _touch_segment(d, "raw", 100)
        _touch_segment(d, "raw", 200)
        _touch_segment(d, "processed", 100)
        finder = SegmentFinder(tmp_path)

        raw = finder.list_segments("c", 1, "raw")
        proc = finder.list_segments("c", 1, "processed")
        assert {s.ts_us for s in raw} == {100, 200}
        assert {s.ts_us for s in proc} == {100}

        # raw track 没有 keypoints
        assert all(s.keypoints_filename is None for s in raw)

    def test_ignores_non_matching_files(self, tmp_path):
        d = _make_task_dir(tmp_path, "c", 1)
        _touch_segment(d, "processed", 100)
        (d / "metadata.json").write_text("{}")
        (d / "raw_playlist.m3u8").write_text("")
        (d / "keypoints_100.json").write_text("[]")
        (d / "garbage.mp4").write_bytes(b"")
        finder = SegmentFinder(tmp_path)

        segs = finder.list_segments("c", 1, "processed")
        assert len(segs) == 1
        assert segs[0].filename == "processed_segment_100.mp4"

    def test_invalid_track_raises(self, tmp_path):
        finder = SegmentFinder(tmp_path)
        with pytest.raises(ValueError, match="Invalid track"):
            finder.list_segments("c", 1, "bogus")


class TestFind:
    @pytest.fixture
    def finder_with_segments(self, tmp_path):
        # 段时间戳（微秒）：1_000_000, 11_000_000, 21_000_000, 31_000_000
        # 即 1s / 11s / 21s / 31s
        d = _make_task_dir(tmp_path, "c1", 1)
        for ts_us in [1_000_000, 11_000_000, 21_000_000, 31_000_000]:
            _touch_segment(d, "processed", ts_us)
        return SegmentFinder(tmp_path)

    def test_find_returns_empty_when_no_segments(self, tmp_path):
        finder = SegmentFinder(tmp_path)
        assert finder.find("c", 1, 1000, "processed") == []

    def test_find_locates_trigger_segment(self, finder_with_segments):
        # ts_ms = 12_000 (12s) → 落在 11s 段内（trigger=11_000_000）
        segs = finder_with_segments.find("c1", 1, ts_ms=12_000, track="processed", n_before=0, n_after=0)
        assert len(segs) == 1
        assert segs[0].ts_us == 11_000_000
        assert segs[0].is_trigger is True

    def test_find_with_context_before_after(self, finder_with_segments):
        # trigger 在 21s，前 1 后 2
        segs = finder_with_segments.find("c1", 1, ts_ms=22_000, track="processed", n_before=1, n_after=2)
        assert [s.ts_us for s in segs] == [11_000_000, 21_000_000, 31_000_000]
        # 触发段标记
        triggers = [s for s in segs if s.is_trigger]
        assert len(triggers) == 1
        assert triggers[0].ts_us == 21_000_000

    def test_find_clamps_at_start(self, finder_with_segments):
        # ts_ms = 500 早于第一段 (1s) → trigger 取第一段
        segs = finder_with_segments.find("c1", 1, ts_ms=500, track="processed", n_before=2, n_after=1)
        # 不会越界到负索引
        assert segs[0].ts_us == 1_000_000
        assert segs[0].is_trigger is True
        assert [s.ts_us for s in segs] == [1_000_000, 11_000_000]

    def test_find_clamps_at_end(self, finder_with_segments):
        # ts_ms = 999_999 远晚于最后一段 → trigger=最后一段
        segs = finder_with_segments.find("c1", 1, ts_ms=999_999, track="processed", n_before=1, n_after=5)
        assert segs[-1].ts_us == 31_000_000
        assert any(s.is_trigger and s.ts_us == 31_000_000 for s in segs)
        # n_after=5 但只有 0 段在后面
        assert len(segs) == 2  # 21s + 31s

    def test_find_exact_boundary_ts_match(self, finder_with_segments):
        # ts_ms = 11_000 (= 11s 段开始) → trigger 应是 11s 段（bisect_right 找 first > ts_us）
        segs = finder_with_segments.find("c1", 1, ts_ms=11_000, track="processed", n_before=0, n_after=0)
        assert len(segs) == 1
        assert segs[0].ts_us == 11_000_000
        assert segs[0].is_trigger

    def test_find_negative_context_rejected(self, finder_with_segments):
        with pytest.raises(ValueError):
            finder_with_segments.find("c1", 1, ts_ms=12_000, track="processed", n_before=-1, n_after=0)
        with pytest.raises(ValueError):
            finder_with_segments.find("c1", 1, ts_ms=12_000, track="processed", n_before=0, n_after=-1)


class TestSegmentRef:
    def test_ts_conversions(self):
        ref = SegmentRef(
            client_id="c", task_id=1, track="raw",
            filename="raw_segment_1234567.mp4", ts_us=1_234_567,
            path=Path("/tmp/x.mp4"),
        )
        assert ref.ts_ms == 1234
        assert abs(ref.ts_s - 1.234567) < 1e-9


class TestBaseDirResolution:
    """读写两侧都应解析到相同的绝对路径，与进程 cwd 无关。"""

    def test_relative_base_dir_resolves_to_project_root_regardless_of_cwd(
        self, tmp_path, monkeypatch
    ):
        from app.services.persistence.config import PersistenceConfig, StorageConfig
        from app.services.traceback.segment_finder import get_default_base_dir

        cfg = PersistenceConfig(storage=StorageConfig(base_dir="./database"))
        write_path = cfg.storage_base_dir
        assert write_path.is_absolute()
        assert write_path.name == "database"

        # 切到完全无关的 cwd，写入端解析的绝对路径必须保持不变
        monkeypatch.chdir(tmp_path)
        assert cfg.storage_base_dir == write_path

        # 读取端（segment_finder.get_default_base_dir）走的也是同一份配置，
        # 在 cwd 已被切到 tmp_path 的条件下，仍应返回 project_root 下的绝对路径
        read_path = get_default_base_dir()
        assert read_path.is_absolute()
        # tmp_path 不可能等于 project_root，所以读出来的路径不会以 tmp_path 开头
        assert tmp_path not in read_path.parents
        assert read_path != tmp_path / "database"

    def test_absolute_base_dir_is_returned_as_is(self, tmp_path):
        from app.services.persistence.config import PersistenceConfig, StorageConfig

        abs_dir = tmp_path / "custom" / "store"
        cfg = PersistenceConfig(storage=StorageConfig(base_dir=str(abs_dir)))
        assert cfg.storage_base_dir == abs_dir.resolve()
