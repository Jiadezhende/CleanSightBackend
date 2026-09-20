"""拉流读超时：`-timeout` 由 settings 出，且与 cleanup_timeout 的串联预算有护栏。

这两件事钉的是同一条实测结论（ffmpeg n7.1.4，冻结中继实验台）：**静默断流下 ffmpeg 要连续
两次读超时才退出，判死延迟 = 2 × `-timeout`**。由此派生出两条本文件要守住的东西：

1. flag 值必须来自 `settings.rtsp_read_timeout_s`（秒 → 微秒），不再硬编码；
2. `2 × T` 全部记在 `cleanup_timeout` 的账上（后者从最后一帧算起），越界要有声音——
   否则进程还没死就先被判 cleanup 拆除，重连永不触发，且**全程无一条日志**。

推导与四档实测值见 `app/services/stream/decoder.py._rtsp_input_opts` 的 docstring。
"""

import logging
from unittest.mock import MagicMock

import pytest

from app.services.health_monitor.config import HealthMonitorConfig
from app.services.health_monitor.manager import GlobalHealthMonitor
from app.services.stream.decoder import _rtsp_input_opts


# ---------------------------------------------------------------------------
# flag 值来自 settings
# ---------------------------------------------------------------------------


class TestRtspInputOpts:
    def _timeout_us(self, opts):
        """取 `-timeout` 紧跟的那个值。用位置而非正则：顺序本身也是契约的一部分。"""
        return opts[opts.index("-timeout") + 1]

    def test_timeout_comes_from_settings_in_microseconds(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.stream.decoder.settings.rtsp_read_timeout_s", 2.5
        )
        assert self._timeout_us(_rtsp_input_opts()) == "2500000"

    def test_timeout_tracks_settings_changes(self, monkeypatch):
        """按调用取值，不是 import 期定死 —— 否则改 env 要重启才生效，且测试无法覆盖。"""
        monkeypatch.setattr(
            "app.services.stream.decoder.settings.rtsp_read_timeout_s", 7.0
        )
        assert self._timeout_us(_rtsp_input_opts()) == "7000000"

    def test_fractional_seconds_do_not_lose_precision_above_us(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.stream.decoder.settings.rtsp_read_timeout_s", 0.25
        )
        assert self._timeout_us(_rtsp_input_opts()) == "250000"

    def test_tcp_transport_is_not_negotiable(self):
        """UDP 下「会话建成却 0 RTP」在进程死活判据下会白等到 cleanup，不自动重启。"""
        opts = _rtsp_input_opts()
        assert opts[opts.index("-rtsp_transport") + 1] == "tcp"


# ---------------------------------------------------------------------------
# 与 cleanup_timeout 的串联预算
# ---------------------------------------------------------------------------


def _monitor(cleanup_timeout: float) -> GlobalHealthMonitor:
    return GlobalHealthMonitor(
        client_manager=MagicMock(),
        stream_service=MagicMock(),
        inference_manager=MagicMock(),
        config=HealthMonitorConfig(cleanup_timeout=cleanup_timeout),
        recording_service=MagicMock(),
    )


class TestReconnectBudgetGuard:
    """`2T` 与 `cleanup_timeout` 分居两处配置，但它们是串联的。

    只告警不纠正：两个值各有正当的运维理由，代码没资格替人选。
    """

    @pytest.mark.parametrize("read_timeout, cleanup", [(2.5, 20.0), (1.0, 20.0)])
    def test_healthy_ratio_is_silent(self, monkeypatch, caplog, read_timeout, cleanup):
        monkeypatch.setattr(
            "app.settings.settings.rtsp_read_timeout_s", read_timeout
        )
        with caplog.at_level(logging.WARNING):
            _monitor(cleanup)._check_reconnect_budget()
        assert caplog.records == []

    def test_over_half_the_budget_warns(self, monkeypatch, caplog):
        """2×6=12s 判死，只剩 8s 给 respawn+建连+等关键帧 —— 线上默认曾是这个形状。"""
        monkeypatch.setattr("app.settings.settings.rtsp_read_timeout_s", 6.0)
        with caplog.at_level(logging.WARNING):
            _monitor(20.0)._check_reconnect_budget()

        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert "过半" in caplog.text

    @pytest.mark.parametrize("read_timeout", [10.0, 15.0])
    def test_at_or_past_the_ceiling_errors(self, monkeypatch, caplog, read_timeout):
        """2T ≥ cleanup_timeout：进程还没死就先被拆除，重连永不触发。

        边界取 `2T == cleanup_timeout` 也算冲突 —— 等号处 cleanup 与判死同 tick，谁先
        由调度决定，不是可以依赖的状态。
        """
        monkeypatch.setattr(
            "app.settings.settings.rtsp_read_timeout_s", read_timeout
        )
        with caplog.at_level(logging.WARNING):
            _monitor(20.0)._check_reconnect_budget()

        assert [r.levelno for r in caplog.records] == [logging.ERROR]
        assert "配置冲突" in caplog.text
