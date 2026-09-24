"""`app.storage.inference`：`{step}/inference/` 下三份推理产物的编解码与读写。

    detections.jsonl      L1 检测结果，路线 B（追加）
    facts.jsonl         L3 时序事实，路线 C（原子整体替换）
    offline_debug.json  离线策略调试件，路线 C

全程用 `tmp_storage` fixture（conftest）把存储根指到临时目录，不碰真实 `database/`。

四类断言，按规范的优先级排：

1. **往返**（T1）：codec 是本域唯一有内容的东西，正反运算必须闭合。detections 侧投影掉的字段
   （extra/metadata）按契约回读为默认值，这是有意有损，也一并钉死；facts 侧无损。
2. **事务不变式**（T3）：路线 C 失败即整体作废——旧文件原样保留、不留 tmp。
3. **落位**：产物只进 `inference/` 子目录，step 根下不留文件——域隔离的执行力。
4. **错误语义**：坏行逐行隔离、形状不对的 record 跳过、IO 失败原样抛。
"""

import json

import numpy as np
import pytest

from app.domain.detection import DetBox, DetectorOutput, FrameDetection
from app.domain.fact import EventFact, SegmentFact
from app.storage import inference, tasks
from app.storage.inference import _detection, _jsonl, _temporal


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------


def _det(bbox=(1, 2, 3, 4), conf=0.9, cls_id=0, cls="person", **extra) -> DetBox:
    return DetBox(bbox=list(bbox), confidence=conf, class_id=cls_id, class_name=cls, **extra)


def _frame(ts, by_source=None, width=1920, height=1080) -> FrameDetection:
    """一帧检测结果。by_source 默认给一个单流单框的最小帧。"""
    if by_source is None:
        by_source = {"cam": [_det()]}
    return FrameDetection(
        ts=ts,
        by_source={
            source: DetectorOutput(boxes=list(dets), metadata={}, timestamp=ts)
            for source, dets in by_source.items()
        },
        frame_width=width,
        frame_height=height,
    )


def _seg(label="brush", start=1.0, end=2.0, producer="offline", **kw) -> SegmentFact:
    return SegmentFact(producer=producer, label=label, start=start, end=end, **kw)


def _evt(signal="birth_rate", value=0.5, ts=1.0, producer="bubble_leak", **kw) -> EventFact:
    return EventFact(producer=producer, signal=signal, value=value, ts=ts, **kw)


def _domain_dir(root, task_id, step_id):
    return root / str(task_id) / str(step_id) / "inference"


def _detections_file(root, task_id, step_id):
    return _domain_dir(root, task_id, step_id) / "detections.jsonl"


def _facts_file(root, task_id, step_id):
    return _domain_dir(root, task_id, step_id) / "facts.jsonl"


def _debug_file(root, task_id, step_id):
    return _domain_dir(root, task_id, step_id) / "offline_debug.json"


# ---------------------------------------------------------------------------
# detections.jsonl：codec 往返（T1）
# ---------------------------------------------------------------------------


class TestDetectionCodec:
    """`_frame_to_record` / `_record_to_frame` 是一对逆运算。"""

    def test_roundtrip_preserves_projected_fields(self):
        src = _frame(1700.5, {"cam": [_det(bbox=(10, 20, 30, 40), conf=0.75, cls_id=3, cls="hand")]})
        got = _detection._record_to_frame(_detection._frame_to_record(src))

        assert got.ts == src.ts
        assert (got.frame_width, got.frame_height) == (1920, 1080)
        assert list(got.by_source) == ["cam"]
        d = got.by_source["cam"].boxes[0]
        assert (d.bbox, d.confidence, d.class_id, d.class_name) == ([10, 20, 30, 40], 0.75, 3, "hand")

    def test_roundtrip_is_lossy_by_contract(self):
        """extra / metadata 刻意不落 —— 离线不消费，且形状不定。

        回读为默认值是**契约**不是 bug；这条用例存在的意义是让改坏它的人看见代价。
        """
        src = _frame(1.0, {"seg": [_det(extra={"k": "v"})]})
        src.by_source["seg"].metadata = {"model": "yolo"}

        got = _detection._record_to_frame(_detection._frame_to_record(src))
        d = got.by_source["seg"].boxes[0]
        assert d.extra == {}
        assert got.by_source["seg"].metadata == {}

    def test_roundtrip_keeps_empty_source(self):
        """"该流这帧没检出" 与 "这帧没有该流" 是两回事，present-key 语义必须保住。"""
        src = _detection._frame_to_record(_frame(1.0, {"cam": [], "ir": [_det()]}))
        got = _detection._record_to_frame(src)
        assert sorted(got.by_source) == ["cam", "ir"]
        assert got.by_source["cam"].boxes == []

    def test_record_timestamp_fans_out_to_every_source(self):
        """同帧多流同源同值：每源 DetectorOutput.timestamp = 记录级 ts。"""
        src = _detection._frame_to_record(_frame(88.25, {"a": [], "b": []}))
        got = _detection._record_to_frame(src)
        assert [fd.timestamp for fd in got.by_source.values()] == [88.25, 88.25]

    def test_numpy_scalars_survive_json(self):
        """bbox/conf/cls_id 强制成原生类型 —— json 不吃 np.int64，检测器给的常是它。"""
        det = _det(bbox=np.array([1, 2, 3, 4]), conf=np.float32(0.5), cls_id=np.int64(2))
        json.dumps(_detection._frame_to_record(_frame(1.0, {"cam": [det]})))  # 不抛即通过

    def test_missing_resolution_restores_as_none(self):
        rec = _detection._frame_to_record(FrameDetection(ts=1.0, by_source={}))
        assert "frame_width" not in rec
        got = _detection._record_to_frame(rec)
        assert got.frame_width is None and got.frame_height is None

    def test_int_ts_from_handwritten_file_becomes_float(self):
        """手写 JSONL 常给整数 ts；反序列化边界统一 float，免得下游比较时类型分叉。"""
        got = _detection._record_to_frame({"ts": 3, "detections": {}})
        assert isinstance(got.ts, float) and got.ts == 3.0


# ---------------------------------------------------------------------------
# detections.jsonl：读写
# ---------------------------------------------------------------------------


class TestDetectionsReadWrite:
    def test_append_read_roundtrip(self, tmp_storage):
        inference.append_detections(1, 2, [_frame(1.0), _frame(2.0)])
        got = inference.read_detections(1, 2)
        assert [f.ts for f in got] == [1.0, 2.0]
        assert got[0].by_source["cam"].boxes[0].bbox == [1, 2, 3, 4]

    def test_append_accumulates_across_calls(self, tmp_storage):
        inference.append_detections(1, 2, [_frame(1.0)])
        inference.append_detections(1, 2, [_frame(2.0), _frame(3.0)])
        assert [f.ts for f in inference.read_detections(1, 2)] == [1.0, 2.0, 3.0]

    def test_read_sorts_by_ts(self, tmp_storage):
        inference.append_detections(1, 2, [_frame(3.0), _frame(1.0), _frame(2.0)])
        assert [f.ts for f in inference.read_detections(1, 2)] == [1.0, 2.0, 3.0]

    def test_read_missing_returns_empty(self, tmp_storage):
        assert inference.read_detections(9, 9) == []

    def test_read_missing_creates_nothing(self, tmp_storage):
        """读一个没写过的 step 不该在盘上留空目录 —— 空目录会被 tasks.list_task_ids() 列出。"""
        inference.read_detections(9, 9)
        assert list(tmp_storage.iterdir()) == []

    def test_empty_batch_writes_nothing(self, tmp_storage):
        """追加零条 = 没事发生：不建目录、不建文件（与 write_facts 的空批语义刻意不同）。"""
        inference.append_detections(1, 2, [])
        assert list(tmp_storage.iterdir()) == []

    def test_writes_into_domain_dir_not_step_root(self, tmp_storage):
        """域隔离：step 根下只有域目录、没有文件。"""
        inference.append_detections(1, 2, [_frame(1.0)])
        assert [p.name for p in (tmp_storage / "1" / "2").iterdir()] == ["inference"]
        assert _detections_file(tmp_storage, 1, 2).is_file()

    def test_steps_are_isolated(self, tmp_storage):
        inference.append_detections(1, 1, [_frame(1.0)])
        inference.append_detections(1, 2, [_frame(2.0), _frame(3.0)])
        assert [f.ts for f in inference.read_detections(1, 1)] == [1.0]
        assert [f.ts for f in inference.read_detections(1, 2)] == [2.0, 3.0]

    def test_tasks_are_isolated(self, tmp_storage):
        inference.append_detections(1, 1, [_frame(1.0)])
        inference.append_detections(2, 1, [_frame(9.0)])
        assert [f.ts for f in inference.read_detections(2, 1)] == [9.0]

    def test_skips_corrupt_line_without_losing_the_rest(self, tmp_storage):
        """JSONL 逐行独立：一行坏了不该让其余几万帧陪葬。"""
        inference.append_detections(1, 2, [_frame(1.0), _frame(2.0)])
        with _detections_file(tmp_storage, 1, 2).open("a", encoding="utf-8") as f:
            f.write("{not json\n\n")
        inference.append_detections(1, 2, [_frame(3.0)])
        assert [f.ts for f in inference.read_detections(1, 2)] == [1.0, 2.0, 3.0]

    def test_skips_valid_json_that_is_not_an_object(self, tmp_storage):
        """`123` / `[1,2]` 都是合法 JSON，但本域每行按契约是一条 record ——
        放行它们只会让 `.get` 在下游炸成 AttributeError。"""
        inference.append_detections(1, 2, [_frame(1.0)])
        with _detections_file(tmp_storage, 1, 2).open("a", encoding="utf-8") as f:
            f.write("123\n[1, 2]\n")
        assert [f.ts for f in inference.read_detections(1, 2)] == [1.0]

    def test_skips_record_with_wrong_shape(self, tmp_storage):
        """能 json.loads 但形状不对（缺 conf）的 record 与坏行同等对待，不中断其余帧。"""
        path = _detections_file(tmp_storage, 1, 2)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"ts": 1.0, "detections": {"cam": [{"bbox": [1, 2, 3, 4]}]}}) + "\n"
            + json.dumps(_detection._frame_to_record(_frame(2.0))) + "\n",
            encoding="utf-8",
        )
        assert [f.ts for f in inference.read_detections(1, 2)] == [2.0]

    def test_tolerates_utf8_bom(self, tmp_storage):
        """Windows 上手写/另存的 detections.jsonl 会带 BOM，读侧必须容忍。"""
        path = _detections_file(tmp_storage, 1, 2)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(_detection._frame_to_record(_frame(5.0))) + "\n",
            encoding="utf-8-sig",
        )
        assert [f.ts for f in inference.read_detections(1, 2)] == [5.0]

    def test_io_failure_propagates(self, tmp_storage):
        """IO 失败原样抛 —— 吞不吞是调用方的策略，本包给不出对两个调用方都对的答案。"""
        (tmp_storage / "1").mkdir()
        (tmp_storage / "1" / "2").write_text("occupied", encoding="utf-8")  # step 目录被文件占位
        with pytest.raises(OSError):
            inference.append_detections(1, 2, [_frame(1.0)])


# ---------------------------------------------------------------------------
# facts.jsonl：codec 往返（T1）
# ---------------------------------------------------------------------------


class TestFactCodec:
    """`_fact_to_record` / `_record_to_fact` 是一对逆运算。两型都**无损**落盘。"""

    def test_segment_roundtrip(self):
        src = _seg(label="long_brush_insert", start=1.5, end=9.25, producer="clean_offline",
                   conf=0.93, meta={"n_frames": 42})
        got = _temporal._record_to_fact(_temporal._fact_to_record(src))
        assert got == src

    def test_event_roundtrip(self):
        src = _evt(signal="state", value={"level": 2}, ts=1700.5, producer="bubble_leak",
                   conf=0.4, meta={"window": 3.0})
        got = _temporal._record_to_fact(_temporal._fact_to_record(src))
        assert got == src

    def test_type_field_discriminates(self):
        """判别字段只活在盘上：内存里靠 isinstance，两型混在一个文件里靠它分开。"""
        assert _temporal._fact_to_record(_seg())["type"] == "segment"
        assert _temporal._fact_to_record(_evt())["type"] == "event"

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError):
            _temporal._record_to_fact({"type": "nope", "producer": "p"})

    def test_missing_type_raises(self):
        with pytest.raises(ValueError):
            _temporal._record_to_fact({"producer": "p", "label": "x", "start": 0, "end": 1})

    def test_optional_fields_restore_to_defaults(self):
        """手写 JSONL 可能不带 conf / meta —— 按 dataclass 默认还原，不炸。"""
        got = _temporal._record_to_fact(
            {"type": "segment", "producer": "p", "label": "x", "start": 0, "end": 1}
        )
        assert got.conf == 1.0 and got.meta == {}

    def test_int_bounds_from_handwritten_file_become_float(self):
        got = _temporal._record_to_fact(
            {"type": "segment", "producer": "p", "label": "x", "start": 0, "end": 1}
        )
        assert isinstance(got.start, float) and isinstance(got.end, float)

    def test_non_fact_raises(self):
        """喂进来不是 Fact 的东西要当场炸，不能静默写出半份文件。"""
        with pytest.raises(TypeError):
            _temporal._fact_to_record({"type": "segment"})


# ---------------------------------------------------------------------------
# facts.jsonl：读写（路线 C）
# ---------------------------------------------------------------------------


class TestFactsReadWrite:
    def test_write_read_roundtrip(self, tmp_storage):
        src = [_seg(label="a"), _evt(signal="s")]
        inference.write_facts(1, 2, src)
        assert inference.read_facts(1, 2) == src

    def test_preserves_disk_order(self, tmp_storage):
        """层**不排序**：两型没有共同时间键，排序依据只能由调用方给。"""
        inference.write_facts(1, 2, [_seg(label="c", start=9.0), _seg(label="a", start=1.0)])
        assert [f.label for f in inference.read_facts(1, 2)] == ["c", "a"]

    def test_write_replaces_the_whole_file(self, tmp_storage):
        """整体替换，不是追加 —— 第二次写之后旧内容一条都不剩。"""
        inference.write_facts(1, 2, [_seg(label="old"), _seg(label="older")])
        inference.write_facts(1, 2, [_seg(label="new")])
        assert [f.label for f in inference.read_facts(1, 2)] == ["new"]

    def test_empty_batch_writes_an_empty_file(self, tmp_storage):
        """「跑过、没分出任何段」与「根本没跑过」在盘上要能分开：前者留一个空文件。"""
        inference.write_facts(1, 2, [])
        assert _facts_file(tmp_storage, 1, 2).is_file()
        assert _facts_file(tmp_storage, 1, 2).read_text(encoding="utf-8") == ""
        assert inference.read_facts(1, 2) == []

    def test_read_missing_returns_empty(self, tmp_storage):
        assert inference.read_facts(9, 9) == []

    def test_read_missing_creates_nothing(self, tmp_storage):
        inference.read_facts(9, 9)
        assert list(tmp_storage.iterdir()) == []

    def test_writes_into_domain_dir_not_step_root(self, tmp_storage):
        inference.write_facts(1, 2, [_seg()])
        assert [p.name for p in (tmp_storage / "1" / "2").iterdir()] == ["inference"]
        assert _facts_file(tmp_storage, 1, 2).is_file()

    def test_leaves_no_tmp_behind(self, tmp_storage):
        """路线 C 的暂存件换名后即消失，盘上不留 `.facts.jsonl.tmp`。"""
        inference.write_facts(1, 2, [_seg()])
        assert [p.name for p in _domain_dir(tmp_storage, 1, 2).iterdir()] == ["facts.jsonl"]

    def test_skips_corrupt_line_without_losing_the_rest(self, tmp_storage):
        inference.write_facts(1, 2, [_seg(label="a")])
        with _facts_file(tmp_storage, 1, 2).open("a", encoding="utf-8") as f:
            f.write("{not json\n")
        assert [f.label for f in inference.read_facts(1, 2)] == ["a"]

    def test_skips_unknown_type_without_losing_the_rest(self, tmp_storage):
        """将来新增第三型时，旧版本读到它是跳过一行，不是整份读不出来。"""
        inference.write_facts(1, 2, [_seg(label="a")])
        with _facts_file(tmp_storage, 1, 2).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "future", "producer": "p"}) + "\n")
        assert [f.label for f in inference.read_facts(1, 2)] == ["a"]

    def test_encode_failure_touches_nothing(self, tmp_storage):
        """整批先编码完再碰盘：序列化炸的时候盘上一个字节没动（W4）。"""
        with pytest.raises(TypeError):
            inference.write_facts(1, 2, [_evt(value=object())])
        assert list(tmp_storage.iterdir()) == []

    def test_failed_replace_keeps_the_old_file(self, tmp_storage, monkeypatch):
        """换名那步失败 = 整体作废：旧文件原样保留、tmp 不残留、原异常上抛（W4）。"""
        inference.write_facts(1, 2, [_seg(label="old")])

        def boom(src, dst):
            raise OSError("disk full")

        # ⚠ 别在这里 monkeypatch.undo()：`tmp_storage` fixture 与本用例共用同一个
        # monkeypatch 实例，undo 会把 settings.storage_dir 一并还原，读侧当场指回真实 database/。
        monkeypatch.setattr(_jsonl.os, "replace", boom)
        with pytest.raises(OSError):
            inference.write_facts(1, 2, [_seg(label="new")])

        assert [f.label for f in inference.read_facts(1, 2)] == ["old"]
        assert [p.name for p in _domain_dir(tmp_storage, 1, 2).iterdir()] == ["facts.jsonl"]


# ---------------------------------------------------------------------------
# offline_debug.json
# ---------------------------------------------------------------------------


class TestDebugResult:
    def test_writes_parsable_json_into_domain_dir(self, tmp_storage):
        inference.write_debug_result(1, 2, {"task_id": 1, "per_frame": [0, 1, 2]})
        assert [p.name for p in (tmp_storage / "1" / "2").iterdir()] == ["inference"]
        got = json.loads(_debug_file(tmp_storage, 1, 2).read_text(encoding="utf-8"))
        assert got == {"task_id": 1, "per_frame": [0, 1, 2]}

    def test_overwrites_previous_run(self, tmp_storage):
        inference.write_debug_result(1, 2, {"run": 1})
        inference.write_debug_result(1, 2, {"run": 2})
        assert json.loads(_debug_file(tmp_storage, 1, 2).read_text(encoding="utf-8")) == {"run": 2}

    def test_unserializable_payload_touches_nothing(self, tmp_storage):
        with pytest.raises(TypeError):
            inference.write_debug_result(1, 2, {"obj": object()})
        assert list(tmp_storage.iterdir()) == []


# ---------------------------------------------------------------------------
# 三份产物互不干扰
# ---------------------------------------------------------------------------


class TestArtifactIsolation:
    def test_write_facts_leaves_detections_alone(self, tmp_storage):
        inference.append_detections(1, 2, [_frame(1.0)])
        inference.write_facts(1, 2, [_seg()])
        assert [f.ts for f in inference.read_detections(1, 2)] == [1.0]

    def test_append_detections_leaves_facts_alone(self, tmp_storage):
        inference.write_facts(1, 2, [_seg(label="a")])
        inference.append_detections(1, 2, [_frame(1.0)])
        assert [f.label for f in inference.read_facts(1, 2)] == ["a"]


# ---------------------------------------------------------------------------
# 整域删除
# ---------------------------------------------------------------------------


class TestDeleteDomain:
    def test_deletes_and_reports_prior_existence(self, tmp_storage):
        inference.append_detections(1, 2, [_frame(1.0)])
        assert inference.delete(1, 2) is True
        assert inference.read_detections(1, 2) == []
        assert inference.delete(1, 2) is False

    def test_takes_all_three_artifacts(self, tmp_storage):
        """supersede 清的是整域：新 run 的检测结果换了，旧 facts 是对旧检测结果的分析，留着即脏数据。"""
        inference.append_detections(1, 2, [_frame(1.0)])
        inference.write_facts(1, 2, [_seg()])
        inference.write_debug_result(1, 2, {"run": 1})

        assert inference.delete(1, 2) is True
        assert not _domain_dir(tmp_storage, 1, 2).exists()

    def test_missing_returns_false_and_creates_nothing(self, tmp_storage):
        assert inference.delete(1, 2) is False
        assert list(tmp_storage.iterdir()) == []

    def test_leaves_other_domains_untouched(self, tmp_storage):
        """只删本域：同 step 的 `hls/` 一个字节都不碰。"""
        inference.append_detections(1, 2, [_frame(1.0)])
        hls_dir = tmp_storage / "1" / "2" / "hls"
        hls_dir.mkdir(parents=True)
        (hls_dir / "raw_playlist.m3u8").write_text("#EXTM3U\n", encoding="utf-8")

        assert inference.delete(1, 2) is True
        assert (hls_dir / "raw_playlist.m3u8").read_text(encoding="utf-8") == "#EXTM3U\n"

    def test_append_after_delete_starts_clean(self, tmp_storage):
        """这正是 supersede 要的：新 run 读到的永远是自己那段完整序列。"""
        inference.append_detections(1, 2, [_frame(1.0)])
        inference.delete(1, 2)
        inference.append_detections(1, 2, [_frame(9.0)])
        assert [f.ts for f in inference.read_detections(1, 2)] == [9.0]


# ---------------------------------------------------------------------------
# 与 tasks 域的接缝
# ---------------------------------------------------------------------------


class TestDomainSeam:
    def test_step_with_only_detections_is_visible_to_tasks(self, tmp_storage):
        """只有 detections.jsonl、没有 HLS 段的 step 必须被 list_step_ids 看见 ——
        它正是 TTL 判据错选 metadata.json 而永不回收的那一类（缺陷 #1）。"""
        inference.append_detections(1, 2, [_frame(1.0)])
        assert tasks.list_step_ids(1) == [2]
        assert tasks.list_task_ids() == [1]

    def test_delete_step_takes_the_whole_domain_with_it(self, tmp_storage):
        """`delete_step` 删的是整个 step，本域三份产物一起没。"""
        inference.append_detections(1, 2, [_frame(1.0)])
        inference.write_facts(1, 2, [_seg()])

        assert tasks.delete_step(1, 2) is True
        assert inference.read_detections(1, 2) == []
        assert inference.read_facts(1, 2) == []
        assert not (tmp_storage / "1").exists()
