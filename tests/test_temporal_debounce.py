"""测试时序分析的边沿触发（去抖）机制

验证 analyze_temporal 的状态机行为：
- rising edge: 条件首次成立 → 产出 1 条 AlarmInfo
- sustained: 条件持续成立 → 不再产出
- falling edge: 条件消失 → 复位
- re-trigger: 条件再次成立 → 重新产出
"""

import time

import pytest

from app.services.inference.data_models import AlarmType, Detection, DetectionOutput


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


# ========== BirthRateAnalyzer 边沿触发测试 ==========


class TestBubbleEdgeTrigger:
    """测试气泡出生率分析器的边沿触发"""

    @pytest.fixture
    def analyzer(self):
        from app.services.inference.workflows.bubble import BirthRateAnalyzer

        return BirthRateAnalyzer(
            birth_rate_threshold=0.5,
            window_seconds=3.0,
        )

    def test_no_alarm_below_threshold(self, analyzer):
        """出生率低于阈值 → 无事件、无告警"""
        window = make_window([0, 0, 0])
        events, alarms = analyzer.analyze_temporal(window)
        assert events == []
        assert alarms == []
        assert analyzer._sm["alarming"] is False

    def test_rising_edge_triggers_alarm(self, analyzer):
        """首次超过阈值 → 产出事件 + 告警，alarming 置 True"""
        # birth_rate > 0.5: 大量新气泡
        window = make_window([5, 5, 5])
        events, alarms = analyzer.analyze_temporal(window)
        assert len(alarms) == 1
        assert alarms[0].alarm_type == AlarmType.PROCESS_VIOLATION
        assert analyzer._sm["alarming"] is True

    def test_sustained_no_repeat_alarm(self, analyzer):
        """条件持续成立 → 有事件但无重复告警"""
        # 第1次：rising edge
        window = make_window([5, 5, 5])
        _, alarms1 = analyzer.analyze_temporal(window)
        assert len(alarms1) == 1

        # 第2次：sustained → alarming 已锁存，不重复产出
        window2 = make_window([5, 5, 5, 5])
        _, alarms2 = analyzer.analyze_temporal(window2)
        assert alarms2 == []

        # 第3次：still sustained
        window3 = make_window([5, 5, 5, 5, 5])
        _, alarms3 = analyzer.analyze_temporal(window3)
        assert alarms3 == []

    def test_falling_edge_resets(self, analyzer):
        """条件消失 → alarming 复位为 False"""
        # rising edge
        analyzer.analyze_temporal(make_window([5, 5, 5]))
        assert analyzer._sm["alarming"] is True

        # 清空历史 + 重置游标，让下一次 tick 的 birth_rate 归零
        analyzer._sm["new_count_history"] = []
        analyzer._sm["last_ts"] = 0.0
        events, alarms = analyzer.analyze_temporal(make_window([0, 0, 0]))
        assert alarms == []
        assert analyzer._sm["alarming"] is False

    def test_empty_window(self, analyzer):
        """空窗口 → 安全返回空"""
        events, alarms = analyzer.analyze_temporal([])
        assert events == []
        assert alarms == []


# ========== DebounceAnalyzer 边沿触发测试 ==========


class TestBendingDebounce:
    """测试弯折去抖分析器的状态机"""

    @pytest.fixture
    def analyzer(self):
        from app.services.inference.workflows.bending import DebounceAnalyzer

        return DebounceAnalyzer(debounce_frames=3, required_bend_actions=4)

    def _make_bending_window(self, flags: list[bool]) -> list[DetectionOutput]:
        """构造弯折窗口，flags 中 True 表示该帧有 bent 检测"""
        base_ts = time.time() - len(flags) * 0.1
        window = []
        for i, has_bending in enumerate(flags):
            n = 1 if has_bending else 0
            output = make_detection_output(n, class_name="bent")
            output.timestamp = base_ts + i * 0.1
            window.append(output)
        return window

    def test_debounce_increments_bend_actions(self, analyzer):
        """连续 debounce_frames 帧 bent → bend_actions 增加"""
        window = self._make_bending_window([True] * 5)
        events, alarms = analyzer.analyze_temporal(window)
        assert analyzer._sm["bend_actions"] >= 1
        assert alarms == []  # 实时阶段不产出告警

    def test_no_transition_below_debounce(self, analyzer):
        """未满去抖帧数 → 不切换状态"""
        window = self._make_bending_window([True, True])  # 2 帧 < debounce_frames=3
        analyzer.analyze_temporal(window)
        assert analyzer._sm["state"] == "STRAIGHT"
        assert analyzer._sm["bend_actions"] == 0

    def test_finalize_alarm_when_insufficient(self, analyzer):
        """终态 bend_actions < required → finalize() 产出 warning"""
        window = self._make_bending_window([True] * 5 + [False] * 5)
        analyzer.analyze_temporal(window)
        # bend_actions 应 < required_bend_actions=4
        alarms = analyzer.finalize()
        assert len(alarms) == 1
        assert alarms[0].alarm_level == "warning"
        assert "弯曲动作不足" in alarms[0].alarm_message

    def test_finalize_no_alarm_when_sufficient(self, analyzer):
        """bend_actions >= required → finalize() 返回空"""
        # 手动置满
        analyzer._sm["bend_actions"] = 4
        alarms = analyzer.finalize()
        assert alarms == []

    def test_state_transitions(self, analyzer):
        """STRAIGHT→BENT→STRAIGHT 完整转换"""
        assert analyzer._sm["state"] == "STRAIGHT"

        # 连续 3 帧 bent → 切换到 BENT
        w1 = self._make_bending_window([True] * 3)
        analyzer.analyze_temporal(w1)
        assert analyzer._sm["state"] == "BENT"

        # 连续 3 帧 straight → 切换回 STRAIGHT
        w2 = self._make_bending_window([False] * 3)
        for item in w2:
            item.timestamp += 0.5  # 确保时间戳在游标之后
        analyzer.analyze_temporal(w2)
        assert analyzer._sm["state"] == "STRAIGHT"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
