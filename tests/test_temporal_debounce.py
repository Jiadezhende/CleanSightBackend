"""测试时序分析的边沿触发（去抖）机制 —— 分层契约（L3 Analyzer 产事实 / L4 Judge 出告警）

验证拆分后状态机行为与拆分前一致：
- L3 Analyzer.run(window) 产出 EventFact（测量），测量状态机在 analyzer._sm
- L4 Judge.step(facts) 做边沿触发判定，决策状态机在 judge._sm
  - rising edge: 条件首次成立 → 产出 1 条 AlarmInfo
  - sustained: 条件持续成立 → 不再产出
  - falling edge: 条件消失 → 复位
- Judge.finalize() 结算告警（弯曲不足）
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


# ========== bubble 边沿触发测试（BubbleAnalyzer + BubbleJudge）==========


class TestBubbleEdgeTrigger:
    """测试气泡出生率 L3 产事实 + L4 上升沿触发"""

    @pytest.fixture
    def pair(self):
        from app.services.inference.workflows.bubble import BubbleAnalyzer, BubbleJudge

        return (
            BubbleAnalyzer(window_seconds=3.0),
            BubbleJudge(birth_rate_threshold=0.5),
        )

    def test_no_alarm_below_threshold(self, pair):
        """出生率低于阈值 → 无事件、无告警"""
        analyzer, judge = pair
        facts = analyzer.run(make_window([0, 0, 0]), online=True)
        events, alarms = judge.step(facts)
        assert events == []
        assert alarms == []
        assert judge._sm["alarming"] is False

    def test_rising_edge_triggers_alarm(self, pair):
        """首次超过阈值 → 产出事件 + 告警，alarming 置 True"""
        analyzer, judge = pair
        facts = analyzer.run(make_window([5, 5, 5]), online=True)
        events, alarms = judge.step(facts)
        assert len(alarms) == 1
        assert alarms[0].alarm_type == AlarmType.PROCESS_VIOLATION
        assert judge._sm["alarming"] is True

    def test_sustained_no_repeat_alarm(self, pair):
        """条件持续成立 → 有事件但无重复告警"""
        analyzer, judge = pair
        # 第1次：rising edge
        _, alarms1 = judge.step(analyzer.run(make_window([5, 5, 5]), online=True))
        assert len(alarms1) == 1

        # 第2次：sustained → alarming 已锁存，不重复产出
        _, alarms2 = judge.step(analyzer.run(make_window([5, 5, 5, 5]), online=True))
        assert alarms2 == []

        # 第3次：still sustained
        _, alarms3 = judge.step(analyzer.run(make_window([5, 5, 5, 5, 5]), online=True))
        assert alarms3 == []

    def test_falling_edge_resets(self, pair):
        """条件消失 → alarming 复位为 False"""
        analyzer, judge = pair
        # rising edge
        judge.step(analyzer.run(make_window([5, 5, 5]), online=True))
        assert judge._sm["alarming"] is True

        # 清空测量历史 + 重置游标，让下一次 tick 的 birth_rate 归零
        analyzer._sm["new_count_history"] = []
        analyzer._sm["last_ts"] = 0.0
        events, alarms = judge.step(analyzer.run(make_window([0, 0, 0]), online=True))
        assert alarms == []
        assert judge._sm["alarming"] is False

    def test_empty_window(self, pair):
        """空窗口 → 安全返回空"""
        analyzer, judge = pair
        facts = analyzer.run([], online=True)
        assert facts == []
        events, alarms = judge.step(facts)
        assert events == []
        assert alarms == []


# ========== bending 去抖测试（BendingAnalyzer + BendingJudge）==========


class TestBendingDebounce:
    """测试弯折去抖 L3 状态机 + L4 结算"""

    @pytest.fixture
    def pair(self):
        from app.services.inference.workflows.bending import BendingAnalyzer, BendingJudge

        return (
            BendingAnalyzer(debounce_frames=3),
            BendingJudge(required_bend_actions=4),
        )

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

    def test_debounce_increments_bend_actions(self, pair):
        """连续 debounce_frames 帧 bent → bend_actions 增加（测量状态在 analyzer._sm）"""
        analyzer, judge = pair
        facts = analyzer.run(self._make_bending_window([True] * 5), online=True)
        _, alarms = judge.step(facts)
        assert analyzer._sm["bend_actions"] >= 1
        assert alarms == []  # 实时阶段不产出告警

    def test_no_transition_below_debounce(self, pair):
        """未满去抖帧数 → 不切换状态"""
        analyzer, judge = pair
        analyzer.run(self._make_bending_window([True, True]), online=True)  # 2 < 3
        assert analyzer._sm["state"] == "STRAIGHT"
        assert analyzer._sm["bend_actions"] == 0

    def test_finalize_alarm_when_insufficient(self, pair):
        """终态 bend_actions < required → finalize() 产出 warning"""
        analyzer, judge = pair
        facts = analyzer.run(
            self._make_bending_window([True] * 5 + [False] * 5), online=True
        )
        judge.step(facts)  # 把 count 事实喂给 judge（结算依据）
        alarms = judge.finalize()
        assert len(alarms) == 1
        assert alarms[0].alarm_level == "warning"
        assert "弯曲动作不足" in alarms[0].alarm_message

    def test_finalize_no_alarm_when_sufficient(self, pair):
        """bend_actions >= required → finalize() 返回空"""
        analyzer, judge = pair
        # 手动置满判定侧计数
        judge._sm["bend_actions"] = 4
        alarms = judge.finalize()
        assert alarms == []

    def test_state_transitions(self, pair):
        """STRAIGHT→BENT→STRAIGHT 完整转换（测量状态机在 analyzer._sm）"""
        analyzer, judge = pair
        assert analyzer._sm["state"] == "STRAIGHT"

        # 连续 3 帧 bent → 切换到 BENT
        analyzer.run(self._make_bending_window([True] * 3), online=True)
        assert analyzer._sm["state"] == "BENT"

        # 连续 3 帧 straight → 切换回 STRAIGHT
        w2 = self._make_bending_window([False] * 3)
        for item in w2:
            item.timestamp += 0.5  # 确保时间戳在游标之后
        analyzer.run(w2, online=True)
        assert analyzer._sm["state"] == "STRAIGHT"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
