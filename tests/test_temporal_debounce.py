"""测试时序分析的边沿触发（去抖）机制

验证 analyze_temporal 的状态机行为：
- rising edge: 条件首次成立 → 产出 1 条 AlarmInfo
- sustained: 条件持续成立 → 不再产出
- falling edge: 条件消失 → 复位
- re-trigger: 条件再次成立 → 重新产出
"""

import time

import pytest

from app.services.client.state import ClientState
from app.services.inference.data_models import Detection, DetectionOutput


# ========== Fixtures ==========


def make_detection_output(n_detections: int = 0, class_name: str = "bubble") -> DetectionOutput:
    """构造 DetectionOutput，指定检测数量"""
    detections = [
        Detection(
            bbox=[0, 0, 100, 100],
            confidence=0.9,
            class_id=0,
            class_name=class_name,
        )
        for _ in range(n_detections)
    ]
    return DetectionOutput(
        detections=detections,
        metadata={"model": "test"},
        timestamp=time.time(),
    )


def make_window(pattern: list[int], class_name: str = "bubble") -> list[DetectionOutput]:
    """按模式构造滑动窗口

    pattern: e.g. [0, 0, 1, 1, 1] — 每个值表示该帧的检测数量
    """
    base_ts = time.time() - len(pattern) * 0.1
    window = []
    for i, count in enumerate(pattern):
        output = make_detection_output(count, class_name)
        output.timestamp = base_ts + i * 0.1
        window.append(output)
    return window


# ========== Bubble 边沿触发测试 ==========


class TestBubbleEdgeTrigger:
    """测试气泡检测的边沿触发"""

    @pytest.fixture
    def task(self):
        from app.services.inference.workflows.bubble import BubbleDetectionTask

        return BubbleDetectionTask(
            model_path="dummy.pt",  # 不会实际加载模型
        )

    @pytest.fixture
    def state(self):
        return ClientState(client_id="test_client", initial_stage="LEAK")

    def test_no_alarm_below_threshold(self, task, state):
        """连续帧不足3帧 → 无事件、无告警"""
        window = make_window([1, 1, 0])  # 尾部有空帧，连续=0
        events, alarms = task.analyze_temporal(window, state)
        assert events == []
        assert alarms == []

    def test_rising_edge_triggers_alarm(self, task, state):
        """首次连续3帧 → 产出事件 + 告警"""
        window = make_window([1, 1, 1])
        events, alarms = task.analyze_temporal(window, state)
        assert len(events) == 1
        assert "连续" in events[0]
        assert len(alarms) == 1
        assert alarms[0].alarm_type == "流程违规"
        assert state.get_counter("bubble_alarming", 0) == 1

    def test_sustained_no_repeat_alarm(self, task, state):
        """条件持续成立 → 有事件但无重复告警"""
        # 第1次：rising edge
        window = make_window([1, 1, 1])
        _, alarms1 = task.analyze_temporal(window, state)
        assert len(alarms1) == 1

        # 第2次：sustained → 无告警
        window = make_window([1, 1, 1, 1])
        events2, alarms2 = task.analyze_temporal(window, state)
        assert len(events2) == 1  # 事件仍然显示
        assert alarms2 == []  # 但不再产出告警

        # 第3次：still sustained
        window = make_window([1, 1, 1, 1, 1])
        _, alarms3 = task.analyze_temporal(window, state)
        assert alarms3 == []

    def test_falling_edge_resets(self, task, state):
        """条件消失 → 复位 alarming 标记"""
        # rising edge
        window = make_window([1, 1, 1])
        task.analyze_temporal(window, state)
        assert state.get_counter("bubble_alarming", 0) == 1

        # falling edge: 尾部无检测
        window = make_window([1, 1, 0])
        events, alarms = task.analyze_temporal(window, state)
        assert alarms == []
        assert state.get_counter("bubble_alarming", 0) == 0  # 已复位

    def test_retrigger_after_reset(self, task, state):
        """条件消失后再次成立 → 重新产出告警"""
        # 第1次 rising edge
        task.analyze_temporal(make_window([1, 1, 1]), state)
        assert state.get_counter("bubble_alarm_count", 0) == 1

        # falling edge
        task.analyze_temporal(make_window([1, 0, 0]), state)

        # 第2次 rising edge → 应再次触发
        _, alarms = task.analyze_temporal(make_window([1, 1, 1]), state)
        assert len(alarms) == 1
        assert state.get_counter("bubble_alarm_count", 0) == 2

    def test_empty_window(self, task, state):
        """空窗口 → 安全返回空"""
        events, alarms = task.analyze_temporal([], state)
        assert events == []
        assert alarms == []


# ========== Bending 边沿触发测试 ==========


class TestBendingEdgeTrigger:
    """测试弯折检测的边沿触发"""

    @pytest.fixture
    def task(self):
        from app.services.inference.workflows.bending import EndoscopeBendingDetectionTask

        return EndoscopeBendingDetectionTask(
            model_path="dummy.pt",
        )

    @pytest.fixture
    def state(self):
        return ClientState(client_id="test_client", initial_stage="LEAK")

    def _make_bending_window(self, flags: list[bool]) -> list[DetectionOutput]:
        """构造弯折窗口，flags 中 True 表示该帧有弯折检测"""
        base_ts = time.time() - len(flags) * 0.1
        window = []
        for i, has_bending in enumerate(flags):
            n = 1 if has_bending else 0
            output = make_detection_output(n, class_name="bent_tube")
            output.timestamp = base_ts + i * 0.1
            window.append(output)
        return window

    def test_below_ratio_no_alarm(self, task, state):
        """弯折比例 < 70% → 无告警"""
        # 10帧中3帧弯折 = 30%
        window = self._make_bending_window(
            [True, False, True, False, True, False, False, False, False, False]
        )
        events, alarms = task.analyze_temporal(window, state)
        assert alarms == []

    def test_rising_edge(self, task, state):
        """首次达到70% → 产出告警"""
        # 10帧中8帧弯折 = 80%
        window = self._make_bending_window(
            [True, True, True, True, True, True, True, True, False, False]
        )
        events, alarms = task.analyze_temporal(window, state)
        assert len(events) == 1
        assert len(alarms) == 1
        assert state.get_counter("bending_alarming", 0) == 1

    def test_sustained_no_repeat(self, task, state):
        """持续弯折 → 有事件但无重复告警"""
        window = self._make_bending_window([True] * 10)

        # rising edge
        _, alarms1 = task.analyze_temporal(window, state)
        assert len(alarms1) == 1

        # sustained
        _, alarms2 = task.analyze_temporal(window, state)
        assert alarms2 == []

    def test_falling_and_retrigger(self, task, state):
        """条件消失后再次成立 → 重新触发"""
        # rising edge
        task.analyze_temporal(self._make_bending_window([True] * 10), state)
        assert state.get_counter("bending_alarm_count", 0) == 1

        # falling edge
        task.analyze_temporal(self._make_bending_window([False] * 10), state)
        assert state.get_counter("bending_alarming", 0) == 0

        # re-trigger
        _, alarms = task.analyze_temporal(self._make_bending_window([True] * 10), state)
        assert len(alarms) == 1
        assert state.get_counter("bending_alarm_count", 0) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
