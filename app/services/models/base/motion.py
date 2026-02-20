"""
Motion Model Service for action analysis in endoscope cleaning process.

This is a no-inference, structure-only default model for testing zero-load scenarios.
It returns empty action results without modifying task state.
"""

from typing import Any, Dict, Optional

from app.models.task import Task


class MotionModel:
    """No-inference default model for zero-load testing."""

    def __init__(self):
        # No model initialization needed for zero-load model
        pass

    def analyze_action(
        self, keypoints: Dict[str, Any], current_task: Task
    ) -> Dict[str, Any]:
        """
        Return minimal action results without task updates.

        This is a zero-load implementation that performs no analysis
        and does not modify task state, suitable for testing system
        performance without inference overhead.

        Args:
            keypoints: Detected keypoints from detection model (unused).
            current_task: Current cleaning task (not modified).

        Returns:
            Dict with empty action analysis results.
        """
        # Return minimal action results without task updates
        actions = {"default": True}

        return actions


# Singleton instance
motion_model = MotionModel()


def analyze_motion(
    keypoints: Dict[str, Any], current_task: Optional[Task]
) -> Dict[str, Any]:
    """
    Wrapper function for motion analysis.

    Args:
        keypoints: Detected keypoints.
        current_task: Current task to update (optional for zero-load model).

    Returns:
        Analysis results.
    """
    # For zero-load model, handle None task gracefully
    if current_task is None:
        return {"default": True}

    return motion_model.analyze_action(keypoints, current_task)
