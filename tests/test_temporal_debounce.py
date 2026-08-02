"""测试流算子 Operator 的边沿触发（去抖）机制 —— analyze 推进状态 / judge 出告警，共享 _sm

验证合并后状态机行为与合并前一致：
- operator.analyze(windows) 消费帧窗（List[FrameFeature]，多流已对齐）、推进共享 _sm
- operator.judge() 做边沿触发判定
  - rising edge: 条件首次成立 → 产出 1 条 AlarmInfo
  - sustained: 条件持续成立 → 不再产出
  - falling edge: 条件消失 → 复位
- operator.finalize() 结算告警（弯曲不足）
"""

import time

import pytest

from factories import make_frame_detections, make_frame_feature
from app.domain.alarm import AlarmType
from app.domain.detection import FrameFeature  # 仅用于类型注解


# ========== Fixtures ==========


def make_window(pattern: list[int], class_name: str = "bubble",
                source: str = "bubble") -> list[FrameFeature]:
    """按模式构造帧窗（List[FrameFeature]，单流已对齐）

    pattern: e.g. [0, 0, 1, 1, 1] — 每个值表示该帧的检测数量
    每帧 FrameFeature.ts 与内层 FrameDetections.timestamp 对齐同值。
    """
    base_ts = time.time() - len(pattern) * 0.1
    window = []
    for i, count in enumerate(pattern):
        ts = base_ts + i * 0.1
        fd = make_frame_detections(n=count, class_name=class_name, ts=ts)
        window.append(make_frame_feature(ts=ts, by_source={source: fd}))
    return window


# ========== bubble 边沿触发测试（BubbleOperator）==========


class TestBubbleEdgeTrigger:
    """测试气泡出生率流算子：analyze 算 birth_rate + judge 上升沿触发"""

    @pytest.fixture
    def op(self):
        from app.services.inference.temporal.impl.bubble import BubbleOperator

        return BubbleOperator(window_seconds=3.0, birth_rate_threshold=0.5)

    def test_no_alarm_below_threshold(self, op):
        """出生率低于阈值 → 无事件、无告警"""
        op.analyze(make_window([0, 0, 0]))
        events, alarms = op.judge()
        assert events == []
        assert alarms == []
        assert op._sm["alarming"] is False

    def test_rising_edge_triggers_alarm(self, op):
        """首次超过阈值 → 产出事件 + 告警，alarming 置 True"""
        op.analyze(make_window([5, 5, 5]))
        events, alarms = op.judge()
        assert len(alarms) == 1
        assert alarms[0].alarm_type == AlarmType.PROCESS_VIOLATION
        assert op._sm["alarming"] is True

    def test_sustained_no_repeat_alarm(self, op):
        """条件持续成立 → 有事件但无重复告警"""
        op.analyze(make_window([5, 5, 5]))
        _, alarms1 = op.judge()
        assert len(alarms1) == 1

        op.analyze(make_window([5, 5, 5, 5]))
        _, alarms2 = op.judge()
        assert alarms2 == []

        op.analyze(make_window([5, 5, 5, 5, 5]))
        _, alarms3 = op.judge()
        assert alarms3 == []

    def test_falling_edge_resets(self, op):
        """条件消失 → alarming 复位为 False"""
        op.analyze(make_window([5, 5, 5]))
        op.judge()
        assert op._sm["alarming"] is True

        # 清空测量历史 + 重置游标，让下一次 birth_rate 归零
        op._sm["new_count_history"] = []
        op._sm["last_ts"] = 0.0
        op.analyze(make_window([0, 0, 0]))
        events, alarms = op.judge()
        assert alarms == []
        assert op._sm["alarming"] is False

    def test_empty_window(self, op):
        """空窗口 → analyze 无副作用，judge 安全返回空"""
        op.analyze([])
        events, alarms = op.judge()
        assert events == []
        assert alarms == []


# ========== bending 去抖测试（BendingOperator）==========


class TestBendingDebounce:
    """测试弯折去抖流算子：analyze 状态机 + finalize 结算（单份 bend_actions）"""

    @pytest.fixture
    def op(self):
        from app.services.inference.temporal.impl.bending import BendingOperator

        return BendingOperator(debounce_frames=3, required_bend_actions=4)

    def _make_bending_window(self, flags: list[bool]) -> list[FrameFeature]:
        """构造弯折帧窗，flags 中 True 表示该帧有 bent 检测（source="bending"）"""
        base_ts = time.time() - len(flags) * 0.1
        window = []
        for i, has_bending in enumerate(flags):
            ts = base_ts + i * 0.1
            n = 1 if has_bending else 0
            fd = make_frame_detections(n=n, class_name="bent", ts=ts)
            window.append(make_frame_feature(ts=ts, by_source={"bending": fd}))
        return window

    def test_debounce_increments_bend_actions(self, op):
        """连续 debounce_frames 帧 bent → bend_actions 增加（共享 _sm）"""
        op.analyze(self._make_bending_window([True] * 5))
        _, alarms = op.judge()
        assert op._sm["bend_actions"] >= 1
        assert alarms == []  # 实时阶段不产出告警

    def test_no_transition_below_debounce(self, op):
        """未满去抖帧数 → 不切换状态"""
        op.analyze(self._make_bending_window([True, True]))  # 2 < 3
        assert op._sm["state"] == "STRAIGHT"
        assert op._sm["bend_actions"] == 0

    def test_finalize_alarm_when_insufficient(self, op):
        """终态 bend_actions < required → finalize() 产出 warning"""
        op.analyze(self._make_bending_window([True] * 5 + [False] * 5))
        op.judge()
        alarms = op.finalize()
        assert len(alarms) == 1
        assert alarms[0].alarm_level == "warning"
        assert "弯曲动作不足" in alarms[0].alarm_message

    def test_finalize_no_alarm_when_sufficient(self, op):
        """bend_actions >= required → finalize() 返回空"""
        op._sm["bend_actions"] = 4
        alarms = op.finalize()
        assert alarms == []

    def test_state_transitions(self, op):
        """STRAIGHT→BENT→STRAIGHT 完整转换（共享 _sm）"""
        assert op._sm["state"] == "STRAIGHT"

        op.analyze(self._make_bending_window([True] * 3))
        assert op._sm["state"] == "BENT"

        w2 = self._make_bending_window([False] * 3)
        for item in w2:
            item.ts += 0.5  # 确保时间戳在游标之后
            item.by_source["bending"].timestamp += 0.5
        op.analyze(w2)
        assert op._sm["state"] == "STRAIGHT"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
