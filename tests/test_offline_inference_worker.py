"""离线时序推理 worker 的最小闭环测试。"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.inference.offline.segmenter import BrushRuleSegmenter
from app.services.inference.offline.worker import (
    OfflineInferenceWorker,
    load_sequence_from_features_jsonl,
    query_timeline,
)


def _write_feature_row(handle, ts: float, clean_large=None, clean_small=None) -> None:
    """写一行 FeatureStore 兼容 JSONL。"""
    handle.write(
        json.dumps(
            {
                "ts": ts,
                "features": {
                    "clean_large": clean_large or [],
                    "clean_small": clean_small or [],
                },
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def test_offline_worker_reads_features_and_writes_timeline(tmp_path: Path):
    """worker 应能读取 features.jsonl，产出 short_brush_cleaning 时间线并写 facts.jsonl。"""
    task_id = 1001
    step_id = 2
    root = tmp_path / str(task_id) / str(step_id)
    root.mkdir(parents=True)
    feature_path = root / "features.jsonl"

    hand = {"bbox": [100, 100, 180, 220], "conf": 0.9, "cls_id": 0, "cls": "hand"}
    scope = {"bbox": [250, 150, 430, 260], "conf": 0.8, "cls_id": 1, "cls": "scope_control_body"}
    short_brush = {"bbox": [180, 190, 250, 215], "conf": 0.85, "cls_id": 6, "cls": "short_brush"}
    air_gun = {"bbox": [300, 190, 360, 230], "conf": 0.88, "cls_id": 5, "cls": "air_gun"}

    with feature_path.open("w", encoding="utf-8") as handle:
        _write_feature_row(handle, 0.0)
        _write_feature_row(handle, 0.1, clean_large=[hand, scope], clean_small=[short_brush])
        _write_feature_row(handle, 0.2, clean_large=[hand, scope], clean_small=[short_brush])
        _write_feature_row(handle, 0.3, clean_large=[hand, scope], clean_small=[short_brush])
        _write_feature_row(handle, 0.4, clean_small=[air_gun])
        _write_feature_row(handle, 0.5, clean_small=[air_gun])

    worker = OfflineInferenceWorker(storage_base_dir=tmp_path, fps=10.0, min_duration_s=0.15)
    result = worker.run(task_id=task_id, step_id=step_id, write_ledger=True)
    sequence = load_sequence_from_features_jsonl(feature_path, task_id=task_id, step_id=step_id, fps=10.0)
    model_input = BrushRuleSegmenter().build_model_input(sequence)

    labels = [segment.label for segment in result.timeline]
    assert "short_brush_cleaning" in labels
    assert "air_injection" in labels
    assert model_input.frame_count == 6
    assert model_input.feature_dim == 62
    assert "short_brush_count" in model_input.feature_names
    assert (root / "offline_inference_result.json").exists()
    assert (root / "facts.jsonl").exists()

    queried = query_timeline(tmp_path, task_id=task_id, step_id=step_id)
    assert [row["label"] for row in queried] == labels


def test_worker_accepts_utf8_bom_features_jsonl(tmp_path: Path):
    """Windows 手工写入的 UTF-8 BOM JSONL 也应能被 worker 读取。"""
    feature_path = tmp_path / "features.jsonl"
    row = {
        "ts": 0.0,
        "features": {
            "clean_large": [{"bbox": [1, 2, 3, 4], "conf": 0.9, "cls_id": 1, "cls": "hand"}],
        },
    }
    feature_path.write_text("\ufeff" + json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    sequence = load_sequence_from_features_jsonl(
        feature_path,
        task_id=1002,
        step_id=2,
        sources=["clean_large"],
        fps=10.0,
    )

    assert len(sequence.frames) == 1
    assert sequence.sources == ["clean_large"]
