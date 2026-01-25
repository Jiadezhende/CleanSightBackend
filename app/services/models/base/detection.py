"""
Detection Model Service for keypoint detection in endoscope cleaning process.

This is a no-inference, structure-only default model for testing zero-load scenarios.
It returns the frame unchanged with minimal metadata.
"""

import numpy as np
from typing import Dict, Any, Tuple


class DetectionModel:
    """No-inference default model for zero-load testing."""

    def __init__(self):
        # No model initialization needed for zero-load model
        pass

    def detect_keypoints(self, frame: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
        """
        Return frame unchanged with minimal keypoints metadata.

        This is a zero-load implementation that performs no processing,
        suitable for testing system performance without inference overhead.

        Args:
            frame: Input frame as numpy array.

        Returns:
            Tuple of (unmodified_frame, empty_keypoints_dict).
        """
        # Return frame as-is with minimal metadata
        keypoints = {"default": True}

        return frame, keypoints


# Singleton instance
detection_model = DetectionModel()


def detect_keypoints(frame: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Convenience function to detect keypoints."""
    return detection_model.detect_keypoints(frame)