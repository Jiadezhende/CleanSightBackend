import numpy as np

from app.services.inference.data_models import Detection, DetectionOutput
from app.services.inference.workflows.mstcn_features import window_to_mstcn_features


def test_window_to_mstcn_features_uses_best_detection_and_xywhn():
    output = DetectionOutput(
        detections=[
            Detection(
                bbox=[0, 0, 20, 10],
                confidence=0.2,
                class_id=0,
                class_name="Hand",
            ),
            Detection(
                bbox=[20, 10, 60, 30],
                confidence=0.9,
                class_id=0,
                class_name="Hand",
            ),
            Detection(
                bbox=[0, 0, 10, 10],
                confidence=0.8,
                class_id=1,
                class_name="long_brush_head",
            ),
        ],
        metadata={"frame_shape": (100, 200, 3)},
        timestamp=1.0,
    )

    features = window_to_mstcn_features([output])

    assert features.shape == (20, 1)
    np.testing.assert_allclose(
        features[0:5, 0],
        np.array([0.2, 0.2, 0.2, 0.2, 0.9], dtype=np.float32),
    )
    np.testing.assert_allclose(
        features[5:10, 0],
        np.array([0.025, 0.05, 0.05, 0.1, 0.8], dtype=np.float32),
    )
