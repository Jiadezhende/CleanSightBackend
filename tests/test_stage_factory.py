import logging

from app.services.inference.config import InferenceConfig
from app.services.inference.stage_factory import StageFactory


def test_build_task_metric_map_warns_once_per_unmapped_model_name(caplog):
    config = InferenceConfig(
        {
            "stages": {
                "CLEAN": {
                    "models": [
                        {"name": "mock_detection", "class": "unused.MockDetector"}
                    ]
                },
                "MOCK": {
                    "models": [
                        {"name": "mock_detection", "class": "unused.MockDetector"}
                    ]
                },
            }
        }
    )

    with caplog.at_level(logging.WARNING):
        mapping = StageFactory(config).build_task_metric_map()

    assert mapping == {}
    warnings = [
        record.message
        for record in caplog.records
        if "has no AlarmMetric mapping" in record.message
    ]
    assert warnings == [
        "[StageFactory] model 'mock_detection' has no AlarmMetric mapping, excluded from signals_10s"
    ]
