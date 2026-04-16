"""
Bug 2 回归测试：初始拉流失败后的重连机制

背景：
  当 start_stream() 在流未就绪时被调用，FFmpeg 启动失败（exit_code≠0）。
  修复前：decoder 注册在 start() 之后，start() 失败 → decoder 不进 self.decoders
          → 健康监控 has_decoder=False → 走 orphan 清理路径 → 不重连。
  修复后：decoder 先注册，再 start()，start() 失败后 decoder 仍在 self.decoders
          → 健康监控 has_decoder=True → 检测到 idle → 进入重连模式 → 自动重试。

测试覆盖：
  1. StreamService：start() 失败后 decoder 必须在 self.decoders 中
  2. GlobalHealthMonitor：已注册的 dead decoder 走重连路径而非 orphan 路径
  3. 对照组：decoder 未注册时走 orphan 路径（演示 bug 原状）
  4. 完整场景：初始失败 → 重连成功的完整状态机
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from app.services.health_monitor.config import HealthMonitorConfig
from app.services.health_monitor.monitor import GlobalHealthMonitor
from app.services.stream.service import StreamService
from app.utils.exceptions import FFmpegError


# ===========================================================================
# 辅助函数
# ===========================================================================

def _make_monitor(client_id: str, mock_cq, active_decoder_ids: set) -> GlobalHealthMonitor:
    """构建一个带 mock 依赖的 GlobalHealthMonitor，用于单元测试。

    Args:
        client_id: 被测客户端 ID
        mock_cq: mock 的 ClientQueues（需设置 latest_raw_timestamp）
        active_decoder_ids: 模拟 stream_service.get_all_client_ids() 的返回值
    """
    mock_cm = MagicMock()
    mock_cm.get_all_clients.return_value = {client_id: mock_cq}

    mock_ss = MagicMock()
    mock_ss.get_all_client_ids.return_value = active_decoder_ids
    mock_ss.get_stream_info.return_value = {
        "url": "rtsp://127.0.0.1:8554/test",
        "fps": 30,
        "protocol": "RTSP",
    }
    mock_ss.restart_stream.return_value = True

    config = HealthMonitorConfig(
        heartbeat_timeout=5.0,
        reconnect_interval=5.0,
        max_reconnect_attempts=5,
        check_interval=1.0,
        orphan_timeout=30.0,
        task_max_duration=0.0,  # 禁用任务超时，避免干扰
    )

    return GlobalHealthMonitor(
        client_manager=mock_cm,
        stream_service=mock_ss,
        inference_manager=MagicMock(),
        config=config,
    )


# ===========================================================================
# Part 1：StreamService — decoder 注册时机
# ===========================================================================

class TestDecoderRegistration:
    """验证 Bug 2 的核心修复：decoder 在 start() 失败后仍注册在 self.decoders 中"""

    def setup_method(self):
        self.service = StreamService()
        self.client_id = "reconnect_test_client"

        # 让 settings 提供 mediamtx 端口，避免属性缺失
        self.mock_settings = MagicMock()
        self.mock_settings.mediamtx_proxy_port = 8554
        self.mock_settings.mediamtx_internal_port = 8554

    def _call_start_impl(self, start_side_effect):
        """调用 _start_stream_impl，mock FFmpegDecoder.start() 的行为"""
        with patch("app.services.stream.service.FFmpegDecoder") as MockDecoder, \
             patch.object(
                 self.service, "_get_or_create_client_queues", return_value=MagicMock()
             ), \
             patch("app.settings.settings", self.mock_settings):

            mock_dec = MockDecoder.return_value
            mock_dec.is_alive.return_value = False
            mock_dec.proc = None
            mock_dec.stream_url = "rtsp://127.0.0.1:8554/test"
            mock_dec.fps = 30
            mock_dec.protocol_opts = []
            mock_dec.start.side_effect = start_side_effect

            yield MockDecoder

    def test_decoder_registered_after_ffmpeg_error(self):
        """start() 抛出 FFmpegError 后，decoder 必须仍在 self.decoders 中"""
        error = FFmpegError(
            message="FFmpeg process failed to start",
            client_id=self.client_id,
            exit_code=1,
        )

        with patch("app.services.stream.service.FFmpegDecoder") as MockDecoder, \
             patch.object(
                 self.service, "_get_or_create_client_queues", return_value=MagicMock()
             ), \
             patch("app.settings.settings", self.mock_settings):

            mock_dec = MockDecoder.return_value
            mock_dec.is_alive.return_value = False
            mock_dec.proc = None
            mock_dec.stream_url = "rtsp://127.0.0.1:8554/test"
            mock_dec.fps = 30
            mock_dec.protocol_opts = []
            mock_dec.start.side_effect = error

            with pytest.raises(FFmpegError):
                self.service._start_stream_impl(
                    self.client_id, "rtsp://127.0.0.1:8554/test", 30, "RTSP"
                )

        # 核心断言：decoder 必须在 dict 中（修复的关键）
        assert self.client_id in self.service.decoders, (
            "start() 失败后 decoder 应已注册在 self.decoders 中，"
            "否则健康监控无法触发重连"
        )

    def test_get_stream_info_available_after_failed_start(self):
        """start() 失败后，get_stream_info() 必须返回流信息（供健康监控重连用）"""
        error = FFmpegError(
            message="stream not available", client_id=self.client_id, exit_code=1
        )

        with patch("app.services.stream.service.FFmpegDecoder") as MockDecoder, \
             patch.object(
                 self.service, "_get_or_create_client_queues", return_value=MagicMock()
             ), \
             patch("app.settings.settings", self.mock_settings):

            mock_dec = MockDecoder.return_value
            mock_dec.is_alive.return_value = False
            mock_dec.proc = None
            mock_dec.stream_url = "rtsp://127.0.0.1:8554/test"
            mock_dec.fps = 30
            mock_dec.protocol_opts = ["-rtsp_transport", "udp"]  # RTSP 协议选项
            mock_dec.start.side_effect = error

            with pytest.raises(FFmpegError):
                self.service._start_stream_impl(
                    self.client_id, "rtsp://127.0.0.1:8554/test", 30, "RTSP"
                )

        info = self.service.get_stream_info(self.client_id)
        assert info is not None, "get_stream_info() 应返回流信息供健康监控重连"
        assert info["url"] == "rtsp://127.0.0.1:8554/test"
        assert info["protocol"] == "RTSP"

    def test_metrics_registered_after_failed_start(self):
        """start() 失败后，self.metrics 中也应有记录"""
        error = FFmpegError(
            message="stream not available", client_id=self.client_id, exit_code=1
        )

        with patch("app.services.stream.service.FFmpegDecoder") as MockDecoder, \
             patch.object(
                 self.service, "_get_or_create_client_queues", return_value=MagicMock()
             ), \
             patch("app.settings.settings", self.mock_settings):

            mock_dec = MockDecoder.return_value
            mock_dec.is_alive.return_value = False
            mock_dec.proc = None
            mock_dec.stream_url = "rtsp://127.0.0.1:8554/test"
            mock_dec.fps = 30
            mock_dec.protocol_opts = []
            mock_dec.start.side_effect = error

            with pytest.raises(FFmpegError):
                self.service._start_stream_impl(
                    self.client_id, "rtsp://127.0.0.1:8554/test", 30, "RTSP"
                )

        assert self.client_id in self.service.metrics


# ===========================================================================
# Part 2：GlobalHealthMonitor — 重连路径 vs orphan 路径
# ===========================================================================

class TestHealthMonitorReconnectPath:
    """验证健康监控在 decoder 已注册但无帧时走重连路径"""

    def _make_stale_cq(self, seconds_ago: float = 10.0) -> MagicMock:
        """创建一个 latest_raw_timestamp 已过期的 ClientQueues mock"""
        mock_cq = MagicMock()
        mock_cq.latest_raw_timestamp = time.time() - seconds_ago
        mock_cq.task_started_at = 0.0
        return mock_cq

    def test_enters_reconnect_when_decoder_registered_but_idle(self):
        """
        修复后行为：decoder 已注册（has_decoder=True）+ 超时无帧 → 进入重连模式
        """
        client_id = "monitor_reconnect_test"
        mock_cq = self._make_stale_cq(seconds_ago=10.0)

        monitor = _make_monitor(
            client_id=client_id,
            mock_cq=mock_cq,
            active_decoder_ids={client_id},  # 修复后：decoder 已注册
        )

        monitor._check_all_clients()

        assert client_id in monitor._reconnecting_clients, (
            "decoder 已注册但超时无帧，应进入重连模式（_reconnecting_clients）"
        )

        # _client_stats 在每轮开始时快照，第二轮才反映本轮进入重连的客户端
        monitor._check_all_clients()
        assert monitor._client_stats["reconnecting"] == 1
        assert monitor._client_stats["orphan_streams"] == 0

    def test_enters_orphan_when_decoder_not_registered(self):
        """
        Bug 原状（对照组）：decoder 未注册（has_decoder=False）→ 走 orphan 路径，无法重连
        """
        client_id = "monitor_orphan_test"
        mock_cq = self._make_stale_cq(seconds_ago=10.0)

        monitor = _make_monitor(
            client_id=client_id,
            mock_cq=mock_cq,
            active_decoder_ids=set(),  # bug 原状：start 失败后 decoder 未注册
        )

        monitor._check_all_clients()

        assert client_id not in monitor._reconnecting_clients, (
            "decoder 未注册时不应进入重连模式"
        )
        assert monitor._client_stats["orphan_streams"] == 1

    def test_no_reconnect_when_frames_are_fresh(self):
        """正常情况：有新帧（idle_time < heartbeat_timeout），不应触发重连"""
        client_id = "monitor_healthy_test"
        mock_cq = self._make_stale_cq(seconds_ago=1.0)  # 1s 前有帧，< heartbeat_timeout=5s

        monitor = _make_monitor(
            client_id=client_id,
            mock_cq=mock_cq,
            active_decoder_ids={client_id},
        )

        monitor._check_all_clients()

        assert client_id not in monitor._reconnecting_clients, (
            "有新帧时不应进入重连模式"
        )


# ===========================================================================
# Part 3：完整状态机场景
# ===========================================================================

class TestFullReconnectScenario:
    """端到端场景：初始失败 → 健康监控检测 → 重连尝试 → 重连成功"""

    def test_full_reconnect_state_machine(self):
        """
        完整重连状态机：
        Round 1: idle_time >= heartbeat_timeout → 进入重连模式
        Round 2: last_attempt_time=0 → 立即触发 restart_stream()
        Round 3: 有新帧 + frame_age < threshold → 退出重连模式
        """
        client_id = "full_scenario_client"
        base_timestamp = time.time() - 10.0  # 10s 前，已超时

        mock_cq = MagicMock()
        mock_cq.latest_raw_timestamp = base_timestamp
        mock_cq.task_started_at = 0.0

        monitor = _make_monitor(
            client_id=client_id,
            mock_cq=mock_cq,
            active_decoder_ids={client_id},
        )

        # Round 1：首次检测到超时 → 进入重连模式
        monitor._check_all_clients()
        assert client_id in monitor._reconnecting_clients, "Round 1: 应进入重连模式"
        state = monitor._reconnecting_clients[client_id]
        assert state.attempt_count == 0, "Round 1: 尚未尝试重连"

        # Round 2：last_attempt_time=0，时间差足够大 → 立即触发 restart_stream
        monitor._check_all_clients()
        assert monitor._stream_service.restart_stream.called, (
            "Round 2: 应已调用 restart_stream()"
        )
        assert monitor._reconnecting_clients[client_id].attempt_count == 1

        # 模拟推流端就绪，新帧到来
        mock_cq.latest_raw_timestamp = time.time()

        # Round 3：检测到新帧 → 退出重连模式
        monitor._check_all_clients()
        assert client_id not in monitor._reconnecting_clients, (
            "Round 3: 有新帧后应退出重连模式"
        )

    def test_reconnect_exhausted_after_max_attempts(self):
        """重连次数耗尽（max_reconnect_attempts=5）后应调用 cleanup"""
        client_id = "exhausted_reconnect_client"
        base_timestamp = time.time() - 10.0

        mock_cq = MagicMock()
        mock_cq.latest_raw_timestamp = base_timestamp
        mock_cq.task_started_at = 0.0

        monitor = _make_monitor(
            client_id=client_id,
            mock_cq=mock_cq,
            active_decoder_ids={client_id},
        )
        # restart_stream 始终失败
        monitor._stream_service.restart_stream.return_value = False

        # Round 1：进入重连模式
        monitor._check_all_clients()
        assert client_id in monitor._reconnecting_clients

        # 模拟耗尽所有重连次数
        state = monitor._reconnecting_clients[client_id]
        state.attempt_count = monitor.max_reconnect_attempts  # 直接设为上限

        # 下一轮检测：attempt_count >= max → 触发 cleanup
        monitor._check_all_clients()
        assert client_id not in monitor._reconnecting_clients, (
            "重连耗尽后应退出重连模式（执行 cleanup）"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
