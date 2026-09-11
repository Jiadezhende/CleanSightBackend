"""`app.storage.feature`：`{step}/features/features.jsonl` 的编解码与读写。

同域的 `facts.jsonl` **本期不归本模块管**（货币定不下来，见模块 docstring），故这里只在
「删自己那份产物」「purge 整个 step」两处手工造它当邻居用。
全程用 `tmp_storage` fixture（conftest）把存储根指到临时目录，不碰真实 `database/`。

三类断言，按规范 §7.5 的优先级排：

1. **往返**（T1）：codec 是本域唯一有内容的东西，正反运算必须闭合；投影掉的字段
   （mask/keypoints/metadata）按契约回读为默认值，这是有意有损，也一并钉死。
2. **落位**：产物只进 `features/` 子目录，step 根下不留文件——域隔离的执行力。
3. **错误语义**：坏行逐行隔离、形状不对的 record 跳过、IO 失败原样抛。
"""

import json

import numpy as np
import pytest

from app.domain.detection import Detection, FrameDetections, FrameFeature
from app.storage import feature, tasks


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------


def _det(bbox=(1, 2, 3, 4), conf=0.9, cls_id=0, cls="person", **extra) -> Detection:
    return Detection(bbox=list(bbox), confidence=conf, class_id=cls_id, class_name=cls, **extra)


def _frame(ts, by_source=None, width=1920, height=1080) -> FrameFeature:
    """一帧特征。by_source 默认给一个单流单框的最小帧。"""
    if by_source is None:
        by_source = {"cam": [_det()]}
    return FrameFeature(
        ts=ts,
        by_source={
            source: FrameDetections(detections=list(dets), metadata={}, timestamp=ts)
            for source, dets in by_source.items()
        },
        frame_width=width,
        frame_height=height,
    )


def _features_file(root, task_id, step_id):
    return root / str(task_id) / str(step_id) / "features" / "features.jsonl"


def _facts_file(root, task_id, step_id):
    return root / str(task_id) / str(step_id) / "features" / "facts.jsonl"


# ---------------------------------------------------------------------------
# codec 往返（T1）
# ---------------------------------------------------------------------------


class TestFeatureCodec:
    """`_feature_to_record` / `_record_to_feature` 是一对逆运算。"""

    def test_roundtrip_preserves_projected_fields(self):
        src = _frame(1700.5, {"cam": [_det(bbox=(10, 20, 30, 40), conf=0.75, cls_id=3, cls="hand")]})
        got = feature._record_to_feature(feature._feature_to_record(src))

        assert got.ts == src.ts
        assert (got.frame_width, got.frame_height) == (1920, 1080)
        assert list(got.by_source) == ["cam"]
        d = got.by_source["cam"].detections[0]
        assert (d.bbox, d.confidence, d.class_id, d.class_name) == ([10, 20, 30, 40], 0.75, 3, "hand")

    def test_roundtrip_is_lossy_by_contract(self):
        """mask / keypoints / extra / metadata 刻意不落 —— 离线不消费，且每帧一张数组太重。

        回读为默认值是**契约**不是 bug；这条用例存在的意义是让改坏它的人看见代价。
        """
        src = _frame(1.0, {"seg": [_det(mask=np.zeros((4, 4)), keypoints=[[1, 2]], extra={"k": "v"})]})
        src.by_source["seg"].metadata = {"model": "yolo"}

        got = feature._record_to_feature(feature._feature_to_record(src))
        d = got.by_source["seg"].detections[0]
        assert d.mask is None and d.keypoints is None and d.extra == {}
        assert got.by_source["seg"].metadata == {}

    def test_roundtrip_keeps_empty_source(self):
        """"该流这帧没检出" 与 "这帧没有该流" 是两回事，present-key 语义必须保住。"""
        got = feature._record_to_feature(feature._feature_to_record(_frame(1.0, {"cam": [], "ir": [_det()]})))
        assert sorted(got.by_source) == ["cam", "ir"]
        assert got.by_source["cam"].detections == []

    def test_record_timestamp_fans_out_to_every_source(self):
        """同帧多流同源同值：每源 FrameDetections.timestamp = 记录级 ts。"""
        got = feature._record_to_feature(feature._feature_to_record(_frame(88.25, {"a": [], "b": []})))
        assert [fd.timestamp for fd in got.by_source.values()] == [88.25, 88.25]

    def test_numpy_scalars_survive_json(self):
        """bbox/conf/cls_id 强制成原生类型 —— json 不吃 np.int64，检测器给的常是它。"""
        det = _det(bbox=np.array([1, 2, 3, 4]), conf=np.float32(0.5), cls_id=np.int64(2))
        json.dumps(feature._feature_to_record(_frame(1.0, {"cam": [det]})))  # 不抛即通过

    def test_missing_resolution_restores_as_none(self):
        rec = feature._feature_to_record(FrameFeature(ts=1.0, by_source={}))
        assert "frame_width" not in rec
        got = feature._record_to_feature(rec)
        assert got.frame_width is None and got.frame_height is None

    def test_int_ts_from_handwritten_file_becomes_float(self):
        """手写 JSONL 常给整数 ts；反序列化边界统一 float，免得下游比较时类型分叉。"""
        got = feature._record_to_feature({"ts": 3, "features": {}})
        assert isinstance(got.ts, float) and got.ts == 3.0


# ---------------------------------------------------------------------------
# features.jsonl：读写
# ---------------------------------------------------------------------------


class TestFeaturesReadWrite:
    def test_append_load_roundtrip(self, tmp_storage):
        src = [_frame(1.0), _frame(2.0)]
        feature.append_features(1, 2, src)
        got = feature.load_features(1, 2)
        assert [f.ts for f in got] == [1.0, 2.0]
        assert got[0].by_source["cam"].detections[0].bbox == [1, 2, 3, 4]

    def test_append_accumulates_across_calls(self, tmp_storage):
        feature.append_features(1, 2, [_frame(1.0)])
        feature.append_features(1, 2, [_frame(2.0), _frame(3.0)])
        assert [f.ts for f in feature.load_features(1, 2)] == [1.0, 2.0, 3.0]

    def test_load_sorts_by_ts(self, tmp_storage):
        feature.append_features(1, 2, [_frame(3.0), _frame(1.0), _frame(2.0)])
        assert [f.ts for f in feature.load_features(1, 2)] == [1.0, 2.0, 3.0]

    def test_load_missing_returns_empty(self, tmp_storage):
        assert feature.load_features(9, 9) == []

    def test_load_missing_creates_nothing(self, tmp_storage):
        """读一个没写过的 step 不该在盘上留空目录 —— 空目录会被 tasks.ids() 列出。"""
        feature.load_features(9, 9)
        assert list(tmp_storage.iterdir()) == []

    def test_empty_batch_writes_nothing(self, tmp_storage):
        """追加零条 = 没事发生：不建目录、不建文件。"""
        feature.append_features(1, 2, [])
        assert list(tmp_storage.iterdir()) == []

    def test_writes_into_domain_dir_not_step_root(self, tmp_storage):
        """域隔离：step 根下只有域目录、没有文件。"""
        feature.append_features(1, 2, [_frame(1.0)])
        step_dir = tmp_storage / "1" / "2"
        assert [p.name for p in step_dir.iterdir()] == ["features"]
        assert _features_file(tmp_storage, 1, 2).is_file()

    def test_steps_are_isolated(self, tmp_storage):
        feature.append_features(1, 1, [_frame(1.0)])
        feature.append_features(1, 2, [_frame(2.0), _frame(3.0)])
        assert [f.ts for f in feature.load_features(1, 1)] == [1.0]
        assert [f.ts for f in feature.load_features(1, 2)] == [2.0, 3.0]

    def test_tasks_are_isolated(self, tmp_storage):
        feature.append_features(1, 1, [_frame(1.0)])
        feature.append_features(2, 1, [_frame(9.0)])
        assert [f.ts for f in feature.load_features(2, 1)] == [9.0]

    def test_skips_corrupt_line_without_losing_the_rest(self, tmp_storage):
        """JSONL 逐行独立：一行坏了不该让其余几万帧陪葬。"""
        feature.append_features(1, 2, [_frame(1.0), _frame(2.0)])
        path = _features_file(tmp_storage, 1, 2)
        with path.open("a", encoding="utf-8") as f:
            f.write("{not json\n\n")
        feature.append_features(1, 2, [_frame(3.0)])
        assert [f.ts for f in feature.load_features(1, 2)] == [1.0, 2.0, 3.0]

    def test_skips_valid_json_that_is_not_an_object(self, tmp_storage):
        """`123` / `[1,2]` 都是合法 JSON，但本域每行按契约是一条 record ——
        放行它们只会让 `.get` 在下游炸成 AttributeError。"""
        feature.append_features(1, 2, [_frame(1.0)])
        with _features_file(tmp_storage, 1, 2).open("a", encoding="utf-8") as f:
            f.write("123\n[1, 2]\n")
        assert [f.ts for f in feature.load_features(1, 2)] == [1.0]

    def test_skips_record_with_wrong_shape(self, tmp_storage):
        """能 json.loads 但形状不对（缺 conf）的 record 与坏行同等对待，不中断其余帧。"""
        path = _features_file(tmp_storage, 1, 2)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"ts": 1.0, "features": {"cam": [{"bbox": [1, 2, 3, 4]}]}}) + "\n"
            + json.dumps(feature._feature_to_record(_frame(2.0))) + "\n",
            encoding="utf-8",
        )
        assert [f.ts for f in feature.load_features(1, 2)] == [2.0]

    def test_tolerates_utf8_bom(self, tmp_storage):
        """Windows 上手写/另存的 features.jsonl 会带 BOM，读侧必须容忍。"""
        path = _features_file(tmp_storage, 1, 2)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(feature._feature_to_record(_frame(5.0))) + "\n",
            encoding="utf-8-sig",
        )
        assert [f.ts for f in feature.load_features(1, 2)] == [5.0]

    def test_io_failure_propagates(self, tmp_storage):
        """IO 失败原样抛 —— 吞不吞是调用方的策略，本包给不出对两个调用方都对的答案。"""
        (tmp_storage / "1").mkdir()
        (tmp_storage / "1" / "2").write_text("occupied", encoding="utf-8")  # step 目录被文件占位
        with pytest.raises(OSError):
            feature.append_features(1, 2, [_frame(1.0)])


class TestRemoveFeatures:
    def test_removes_and_reports_prior_existence(self, tmp_storage):
        feature.append_features(1, 2, [_frame(1.0)])
        assert feature.remove_features(1, 2) is True
        assert feature.load_features(1, 2) == []
        assert feature.remove_features(1, 2) is False

    def test_missing_returns_false_and_creates_nothing(self, tmp_storage):
        assert feature.remove_features(1, 2) is False
        assert list(tmp_storage.iterdir()) == []

    def test_leaves_the_rest_of_the_domain_untouched(self, tmp_storage):
        """supersede 清的是特征序列，同域的别的产物（facts.jsonl）与域目录本身都不该动。

        facts.jsonl 本期不归本模块管（货币未定，见模块 docstring），所以这里手工造它——
        正因为本模块不认识它，才更要钉死"删自己那份"这条边界。
        """
        feature.append_features(1, 2, [_frame(1.0)])
        facts = _facts_file(tmp_storage, 1, 2)
        facts.write_text('{"type": "event"}\n', encoding="utf-8")

        assert feature.remove_features(1, 2) is True
        assert facts.read_text(encoding="utf-8") == '{"type": "event"}\n'
        assert facts.parent.is_dir()

    def test_append_after_remove_starts_clean(self, tmp_storage):
        """这正是 supersede 要的：新 run 读到的永远是自己那段完整序列。"""
        feature.append_features(1, 2, [_frame(1.0)])
        feature.remove_features(1, 2)
        feature.append_features(1, 2, [_frame(9.0)])
        assert [f.ts for f in feature.load_features(1, 2)] == [9.0]


# ---------------------------------------------------------------------------
# 与 tasks 域的接缝
# ---------------------------------------------------------------------------


class TestDomainSeam:
    def test_step_with_only_features_is_visible_to_tasks(self, tmp_storage):
        """只有 features.jsonl、没有 HLS 段的 step 必须被 steps() 看见 ——
        它正是 TTL 判据错选 metadata.json 而永不回收的那一类（缺陷 #1）。"""
        feature.append_features(1, 2, [_frame(1.0)])
        assert tasks.steps(1) == [2]
        assert tasks.ids() == [1]

    def test_purge_step_takes_the_whole_domain_with_it(self, tmp_storage):
        """`purge_step` 删的是整个 step，本域连同尚未迁移的 facts.jsonl 一起没。"""
        feature.append_features(1, 2, [_frame(1.0)])
        _facts_file(tmp_storage, 1, 2).write_text('{"n": 1}\n', encoding="utf-8")

        assert tasks.purge_step(1, 2) is True
        assert feature.load_features(1, 2) == []
        assert not (tmp_storage / "1").exists()
