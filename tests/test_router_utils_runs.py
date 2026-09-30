"""`app.routers.utils.runs.resolve_timeline`：取 run 与该轨媒体轴，拿不到就 404。

404 的文案与 `resource_id` 是对外错误体的一部分，与 ai `/temporal`、lab `/label_probs` 现有响应逐字一致。
"""

import pytest

from app.routers.utils.runs import resolve_timeline
from app.types.exceptions import NotFoundError
from factories import make_run, seed_hls_segments

TS0 = 1_700_000_000_000_000        # 首段墙钟起点（us）


def _seed(items, track="raw"):
    return seed_hls_segments(1, 2, items, track=track)


class TestResolveTimeline:
    def test_latest_visible_run_and_its_track(self, tmp_storage):
        _seed([(TS0, 10.0), (TS0 + 10_000_000, 4.0)])

        run, tl = resolve_timeline(1, 2, None, "raw")

        assert run == make_run(1, 2)
        assert tl.duration_ms == 14_000

    def test_named_run(self, tmp_storage):
        _seed([(TS0, 10.0)])
        target = make_run(1, 2)

        run, tl = resolve_timeline(1, 2, target.run_id, "raw")

        assert run == target and len(tl) == 1

    def test_no_visible_run_is_segments_404(self, tmp_storage):
        with pytest.raises(NotFoundError) as exc:
            resolve_timeline(1, 2, None, "raw")

        err = exc.value
        assert err.message == "No raw segments for task 1 step 2"
        assert err.resource_type == "Segments"
        assert err.resource_id == "task=1,step=2,track=raw"

    def test_run_without_segments_on_that_track_is_segments_404(self, tmp_storage):
        _seed([(TS0, 10.0)], track="raw")

        with pytest.raises(NotFoundError) as exc:
            resolve_timeline(1, 2, None, "processed")

        assert exc.value.message == "No processed segments for task 1 step 2"
        assert exc.value.resource_id == "task=1,step=2,track=processed"

    def test_named_missing_run_is_run_404(self, tmp_storage):
        """点名不中走 `resolve_run` 的 Run 404，不是 Segments 404。"""
        _seed([(TS0, 10.0)])

        with pytest.raises(NotFoundError) as exc:
            resolve_timeline(1, 2, 12345, "raw")

        assert exc.value.resource_type == "Run"
        assert exc.value.resource_id == "task=1,step=2,run=12345"
