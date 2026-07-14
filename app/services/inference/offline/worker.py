"""离线时序推理 worker。

一期目标：
    单独启动 worker，读取 FeatureStore 产出的 features.jsonl，调用通用时序模型接口，
    产出结构化行为时间线，并可选写入 FactLedger。

输入：
    1. 后端默认存储路径：
       {storage_base_dir}/{task_id}/{step_id}/features.jsonl
    2. 或手动指定：
       --features-jsonl path/to/features.jsonl

输出：
    1. offline_inference_result.json：完整结构化推理结果，包含逐帧预测和 timeline。
    2. facts.jsonl：可选，写入后端已有 FactLedger 格式，便于后续查询。

示例：
    python -m app.services.inference.offline.worker run --task-id 1001 --step-id 2 \
        --storage-base-dir database --source clean_large --source clean_small --write-ledger

    python -m app.services.inference.offline.worker query --task-id 1001 --step-id 2 \
        --storage-base-dir database
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence

from app.domain.detection import Detection
from app.services.inference.feature.store import FactLedger
from app.services.inference.models import SegmentFact
from app.services.inference.offline.interfaces import (
    FramePrediction,
    OfflineFeatureSequence,
    OfflineFrame,
    OfflineInferenceResult,
    OfflineTemporalModel,
    TimelineSegment,
)
from app.services.inference.offline.segmenter import BrushRuleSegmenter, create_segmenter


DEFAULT_SOURCES = ["clean_large", "clean_small"]
DEFAULT_RESULT_FILE = "offline_inference_result.json"


def _as_sources(values: Iterable[str] | None) -> list[str]:
    """解析 CLI source 参数，支持重复传入或逗号分隔。"""
    if not values:
        return list(DEFAULT_SOURCES)
    sources: list[str] = []
    for raw in values:
        sources.extend([part.strip() for part in raw.split(",") if part.strip()])
    return sources or list(DEFAULT_SOURCES)


def _json_to_detection(row: dict[str, object]) -> Detection:
    """把 features.jsonl 中的检测 dict 还原为 Detection。"""
    bbox = row.get("bbox") or [0, 0, 0, 0]
    return Detection(
        bbox=[int(round(float(v))) for v in list(bbox)[:4]],
        confidence=float(row.get("conf", row.get("confidence", 0.0))),
        class_id=int(row.get("cls_id", row.get("class_id", 0))),
        class_name=str(row.get("cls", row.get("class_name", "unknown"))),
    )


def load_sequence_from_features_jsonl(
    features_jsonl: Path,
    task_id: int,
    step_id: int,
    sources: Sequence[str] | None = None,
    fps: float = 7.5,
) -> OfflineFeatureSequence:
    """读取 features.jsonl 并构造模型输入序列。

    features.jsonl 每行格式由 FeatureStore.append 生成：
        {"ts": 0.0, "features": {"clean_large": [...], "clean_small": [...]}}

    Args:
        features_jsonl: FeatureStore 文件路径。
        task_id: 业务任务 id。
        step_id: 业务步骤 id。
        sources: 需要读取的检测 source；None 表示读取每行中出现的所有 source。
        fps: 特征序列采样帧率，用于估计最后一帧结束时间。

    Returns:
        OfflineFeatureSequence，供 OfflineTemporalModel.predict 使用。
    """
    if not features_jsonl.exists():
        raise FileNotFoundError(f"features.jsonl 不存在: {features_jsonl}")

    wanted = set(sources or [])
    discovered_sources: set[str] = set()
    frames: list[OfflineFrame] = []

    # utf-8-sig 兼容 Windows PowerShell 手工写入的 UTF-8 BOM；FeatureStore
    # 自身写出的无 BOM JSONL 也能按普通 UTF-8 正常读取。
    with features_jsonl.open("r", encoding="utf-8-sig") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{features_jsonl}:{line_no} 不是合法 JSON") from exc

            feature_map = record.get("features") or {}
            if not isinstance(feature_map, dict):
                continue

            detections_by_source: dict[str, list[Detection]] = {}
            for source, rows in feature_map.items():
                source_name = str(source)
                if wanted and source_name not in wanted:
                    continue
                discovered_sources.add(source_name)
                detections_by_source[source_name] = [
                    _json_to_detection(row) for row in rows if isinstance(row, dict)
                ]

            if detections_by_source:
                frames.append(
                    OfflineFrame(
                        timestamp=float(record.get("ts", len(frames) / max(fps, 1e-6))),
                        detections_by_source=detections_by_source,
                    )
                )

    frames.sort(key=lambda frame: frame.timestamp)
    return OfflineFeatureSequence(
        task_id=task_id,
        step_id=step_id,
        frames=frames,
        sources=sorted(discovered_sources),
        fps=fps,
        meta={"features_jsonl": str(features_jsonl)},
    )


def _estimate_end_times(predictions: Sequence[FramePrediction], fps: float) -> list[float]:
    """估计每帧的右边界时间，最后一帧用中位帧间隔或 fps 补齐。"""
    if not predictions:
        return []
    timestamps = [p.timestamp for p in predictions]
    deltas = [
        max(1e-6, timestamps[idx + 1] - timestamps[idx])
        for idx in range(len(timestamps) - 1)
        if timestamps[idx + 1] > timestamps[idx]
    ]
    default_delta = median(deltas) if deltas else 1.0 / max(fps, 1e-6)
    ends = timestamps[1:] + [timestamps[-1] + default_delta]
    return [float(x) for x in ends]


def predictions_to_timeline(
    predictions: Sequence[FramePrediction],
    model_source: str,
    fps: float,
    min_duration_s: float = 0.2,
) -> list[TimelineSegment]:
    """把逐帧预测合并为连续行为时间线，idle 不输出。"""
    if not predictions:
        return []

    end_times = _estimate_end_times(predictions, fps)
    timeline: list[TimelineSegment] = []
    start_idx = 0
    current = predictions[0].label

    for idx in range(1, len(predictions) + 1):
        next_label = predictions[idx].label if idx < len(predictions) else None
        if next_label == current:
            continue

        if current != "idle":
            start = predictions[start_idx].timestamp
            end = end_times[idx - 1]
            if end - start >= min_duration_s:
                conf_values = [p.confidence for p in predictions[start_idx:idx]]
                timeline.append(
                    TimelineSegment(
                        label=current,
                        start=round(float(start), 6),
                        end=round(float(end), 6),
                        confidence=round(sum(conf_values) / max(len(conf_values), 1), 5),
                        start_frame=start_idx + 1,
                        end_frame=idx,
                        source=model_source,
                        meta={"min_duration_s": min_duration_s},
                    )
                )

        start_idx = idx
        current = next_label if next_label is not None else "idle"

    return timeline


def timeline_to_segment_facts(
    timeline: Sequence[TimelineSegment],
    model_version: str,
) -> list[SegmentFact]:
    """把 timeline 转成后端已有 SegmentFact 契约。"""
    return [
        SegmentFact(
            source=segment.source,
            label=segment.label,
            start=segment.start,
            end=segment.end,
            conf=segment.confidence,
            meta={
                **segment.meta,
                "model_version": model_version,
                "start_frame": segment.start_frame,
                "end_frame": segment.end_frame,
            },
        )
        for segment in timeline
    ]


def _fact_path(storage_base_dir: Path, task_id: int, step_id: int) -> Path:
    """FactLedger 的落盘路径，与 app.services.inference.feature.store 保持一致。"""
    return Path(storage_base_dir) / str(task_id) / str(step_id) / "facts.jsonl"


def replace_model_segments(
    storage_base_dir: Path,
    task_id: int,
    step_id: int,
    facts: Sequence[SegmentFact],
    model_source: str,
    model_version: str,
) -> dict[str, int]:
    """用当前模型输出替换同 source + model_version 的旧 SegmentFact。

    FactLedger 当前只有 append/load；为了手动重复运行时结果可读，这里做一个轻量替换。
    """
    path = _fact_path(storage_base_dir, task_id, step_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept: list[dict[str, object]] = []
    removed = 0

    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                row = json.loads(line)
                meta = row.get("meta") or {}
                same_model = (
                    row.get("type") == "segment"
                    and row.get("source") == model_source
                    and isinstance(meta, dict)
                    and meta.get("model_version") == model_version
                )
                if same_model:
                    removed += 1
                else:
                    kept.append(row)

    rows = [fact.to_json() for fact in facts]
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for row in kept + rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temp_path.replace(path)
    return {"kept": len(kept), "removed": removed, "written": len(rows)}


class OfflineInferenceWorker:
    """单次离线推理 worker。

    worker 不依赖在线 client/CQ，不连接数据库。它只读取 FeatureStore 文件并写本地结果。
    """

    def __init__(
        self,
        storage_base_dir: Path,
        model: OfflineTemporalModel | None = None,
        fps: float = 7.5,
        min_duration_s: float = 0.2,
    ):
        self.storage_base_dir = Path(storage_base_dir)
        self.model = model or BrushRuleSegmenter()
        self.fps = float(fps)
        self.min_duration_s = float(min_duration_s)

    def default_features_path(self, task_id: int, step_id: int) -> Path:
        """按后端存储约定计算 features.jsonl 路径。"""
        return self.storage_base_dir / str(task_id) / str(step_id) / "features.jsonl"

    def default_result_path(self, task_id: int, step_id: int) -> Path:
        """按后端存储约定计算结构化结果路径。"""
        return self.storage_base_dir / str(task_id) / str(step_id) / DEFAULT_RESULT_FILE

    def run(
        self,
        task_id: int,
        step_id: int,
        sources: Sequence[str] | None = None,
        features_jsonl: Path | None = None,
        output_json: Path | None = None,
        write_ledger: bool = False,
    ) -> OfflineInferenceResult:
        """执行一次离线推理任务。"""
        feature_path = Path(features_jsonl) if features_jsonl else self.default_features_path(task_id, step_id)
        sequence = load_sequence_from_features_jsonl(
            feature_path,
            task_id=task_id,
            step_id=step_id,
            sources=list(sources) if sources else None,
            fps=self.fps,
        )
        frame_predictions = self.model.predict(sequence)
        timeline = predictions_to_timeline(
            frame_predictions,
            model_source=self.model.name,
            fps=sequence.fps,
            min_duration_s=self.min_duration_s,
        )
        result = OfflineInferenceResult(
            task_id=task_id,
            step_id=step_id,
            model_name=self.model.name,
            model_version=self.model.version,
            sources=sequence.sources,
            frame_count=len(sequence.frames),
            frame_predictions=frame_predictions,
            timeline=timeline,
        )

        result_path = Path(output_json) if output_json else self.default_result_path(task_id, step_id)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")

        if write_ledger:
            facts = timeline_to_segment_facts(timeline, model_version=self.model.version)
            replace_model_segments(
                storage_base_dir=self.storage_base_dir,
                task_id=task_id,
                step_id=step_id,
                facts=facts,
                model_source=self.model.name,
                model_version=self.model.version,
            )

        return result


def query_timeline(
    storage_base_dir: Path,
    task_id: int,
    step_id: int,
    source: str | None = None,
) -> list[dict[str, object]]:
    """从 FactLedger 查询已经写入的行为时间线。"""
    ledger = FactLedger(storage_base_dir)
    rows: list[dict[str, object]] = []
    for fact in ledger.load(task_id, step_id):
        if not isinstance(fact, SegmentFact):
            continue
        if source and fact.source != source:
            continue
        rows.append(fact.to_json())
    rows.sort(key=lambda row: (float(row.get("start", 0.0)), str(row.get("label", ""))))
    return rows


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="手动运行/查询离线时序推理 worker")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="读取 features.jsonl 并产出行为时间线")
    run.add_argument("--task-id", type=int, required=True)
    run.add_argument("--step-id", type=int, required=True)
    run.add_argument("--storage-base-dir", type=Path, default=Path("database"))
    run.add_argument("--features-jsonl", type=Path, default=None)
    run.add_argument("--output-json", type=Path, default=None)
    run.add_argument("--source", action="append", help="检测 source，可重复传或用逗号分隔")
    run.add_argument("--fps", type=float, default=7.5)
    run.add_argument("--min-duration-s", type=float, default=0.2)
    run.add_argument("--model", default="brush_rule", help="离线模型名称，默认 brush_rule")
    run.add_argument("--write-ledger", action="store_true", help="写入/替换 facts.jsonl 中的 SegmentFact")

    query = sub.add_parser("query", help="查询 facts.jsonl 中的行为时间线")
    query.add_argument("--task-id", type=int, required=True)
    query.add_argument("--step-id", type=int, required=True)
    query.add_argument("--storage-base-dir", type=Path, default=Path("database"))
    query.add_argument("--source", default=None, help="只查询某个 SegmentFact source")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        worker = OfflineInferenceWorker(
            storage_base_dir=args.storage_base_dir,
            model=create_segmenter(args.model),
            fps=args.fps,
            min_duration_s=args.min_duration_s,
        )
        result = worker.run(
            task_id=args.task_id,
            step_id=args.step_id,
            sources=_as_sources(args.source),
            features_jsonl=args.features_jsonl,
            output_json=args.output_json,
            write_ledger=args.write_ledger,
        )
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "query":
        rows = query_timeline(
            storage_base_dir=args.storage_base_dir,
            task_id=args.task_id,
            step_id=args.step_id,
            source=args.source,
        )
        print(json.dumps({"task_id": args.task_id, "step_id": args.step_id, "timeline": rows}, ensure_ascii=False, indent=2))
        return 0

    parser.error(f"未知命令: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
