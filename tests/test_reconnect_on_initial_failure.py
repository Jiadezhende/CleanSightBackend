"""
初始拉流失败 + 断流重连 回归测试（进程死活判据版）。

背景（重构后）：
  健康监控的重连判据 = **decoder 子进程死活**（`stream_service.is_decoder_alive`），不再是帧
  staleness。原因：实测 RTSP 断流时后端 ffmpeg 从 TCP 控制通道即收 EOF 退出（`-timeout` 兜底
  把真挂死也转成退出），故「进程死」= 断流/崩溃/首启失败(该 respawn)，「进程活但无帧」= 正在
  等首个关键帧/瞬时停(该等，绝不杀)。放弃(cleanup) = 纯时间触发（无帧 ≥ cleanup_timeout，
  直配 20s），不再数重连次数（`max_reconnect_attempts` 已随派生式一并删除）。

测试覆盖：
  1. StreamService：start() 失败后 decoder 必须仍在 self.decoders（供监控接管）
  2. GlobalHealthMonitor：进程死 → 重连路径；进程活 → 不重连；未注册 → orphan 路径
  3. 完整状态机：进程死 → respawn → 来帧退出重连
  4. 放弃：无帧超 cleanup_timeout → cleanup（时间触发，非次数）
  5. 对象身份 fence：槽位被 /start 换新 run 时放弃重连
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from app.services.health_monitor.config import HealthMonitorConfig
from app.services.health_monitor.manager import GlobalHealthMonitor
from app.services.stream.manager import StreamService
from app.utils.exceptions import FFmpegError


# ===========================================================================
# 辅助函数
# ===========================================================================

def _make_monitor(
    client_id: str,
    mock_cq,
    active_decoder_ids: set,
    *,
    decoder_alive: bool = True,
) -> GlobalHealthMonitor:
    """构建一个带 mock 依赖的 GlobalHealthMonitor，用于单元测试。

    Args:
        client_id: 被测客户端 ID
        mock_cq: mock 的 ClientQueues（需设置 latest_raw_timestamp）
        active_decoder_ids: 模拟 stream_service.get_all_task_ids()（注册即算，不看死活）
        decoder_alive: 模拟 stream_service.is_decoder_alive() 返回值（新判据核心）
    """
    mock_cm = MagicMock()
    mock_cm.snapshot.return_value = {client_id: mock_cq}

    mock_ss = MagicMock()
    mock_ss.get_all_task_ids.return_value = active_decoder_ids
    mock_ss.is_decoder_alive.return_value = decoder_alive
    mock_ss.get_stream_info.return_value = {
        "url": "rtsp://127.0.0.1:8554/test",
    }
    mock_ss.restart_stream.return_value = True

    config = HealthMonitorConfig(
        heartbeat_timeout=5.0,
        reconnect_interval=5.0,
        cleanup_timeout=20.0,  # 放弃时限，直配（不再由 attempts×interval 派生）
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
# Part 1：StreamService — decoder 注册时机（start() 失败后仍注册）
# ===========================================================================

class TestDecoderRegistration:
    """decoder 在 start() 失败后仍注册在 self.decoders 中（供健康监控接管重连）。"""

    def setup_method(self):
        self.service = StreamService()
        self.client_id = "reconnect_test_client"

        self.mock_settings = MagicMock()
        self.mock_settings.mediamtx_proxy_port = 8554
        self.mock_settings.mediamtx_internal_port = 8554

    def _start_with_failing_decoder(self, error):
        with patch("app.services.stream.manager.FFmpegDecoder") as MockDecoder, \
             patch.object(
                 self.service, "_get_client_queues", return_value=MagicMock()
             ), \
             patch("app.settings.settings", self.mock_settings):

            mock_dec = MockDecoder.return_value
            mock_dec.is_alive.return_value = False
            mock_dec.proc = None
            mock_dec.stream_url = "rtsp://127.0.0.1:8554/test"
            mock_dec.start.side_effect = error

            # start() 失败不向上抛异常，由健康监控接管重连
            self.service._start_stream_impl(
                self.client_id, "rtsp://127.0.0.1:8554/test"
            )

    def test_decoder_registered_after_ffmpeg_error(self):
        """start() 失败后 decoder 必须仍在 self.decoders 中。"""
        self._start_with_failing_decoder(
            FFmpegError(message="FFmpeg process failed to start",
                        source_ip=self.client_id, exit_code=1)
        )
        assert self.client_id in self.service.decoders

    def test_get_stream_info_available_after_failed_start(self):
        """start() 失败后 get_stream_info() 仍返回流信息（供重连用）。"""
        self._start_with_failing_decoder(
            FFmpegError(message="stream not available",
                        source_ip=self.client_id, exit_code=1)
        )
        info = self.service.get_stream_info(self.client_id)
        assert info is not None
        assert info["url"] == "rtsp://127.0.0.1:8554/test"

    def test_metrics_registered_after_failed_start(self):
        """start() 失败后 self.metrics 中也应有记录。"""
        self._start_with_failing_decoder(
            FFmpegError(message="stream not available",
                        source_ip=self.client_id, exit_code=1)
        )
        assert self.client_id in self.service.metrics

    def test_is_decoder_alive_false_for_dead_or_missing(self):
        """is_decoder_alive：注册但进程死 → False；未注册 → False。"""
        # 未注册
        assert self.service.is_decoder_alive(self.client_id) is False
        # 注册但 dead（首启失败后的状态）
        self._start_with_failing_decoder(
            FFmpegError(message="x", source_ip=self.client_id, exit_code=1)
        )
        assert self.service.is_decoder_alive(self.client_id) is False


# ===========================================================================
# Part 2：GlobalHealthMonitor — 进程死活判据（重连 vs 只等 vs orphan）
# ===========================================================================

class TestHealthMonitorReconnectPath:
    """进程死 → 重连；进程活 → 只等（不看帧 staleness）；未注册 → orphan。"""

    def _cq(self, seconds_ago: float = 10.0) -> MagicMock:
        cq = MagicMock()
        cq.latest_raw_timestamp = time.time() - seconds_ago
        cq.task_started_at = 0.0
        return cq

    def test_enters_reconnect_when_decoder_process_dead(self):
        """decoder 已注册但进程已退出（is_decoder_alive=False）→ 进入重连模式。"""
        client_id = "monitor_reconnect_test"
        mock_cq = self._cq(seconds_ago=10.0)

        monitor = _make_monitor(
            client_id, mock_cq, active_decoder_ids={client_id}, decoder_alive=False
        )

        monitor._check_all_clients()
        assert client_id in monitor._reconnecting_clients, (
            "decoder 进程已死，应进入重连模式（_reconnecting_clients）"
        )

        # _client_stats 在每轮开始快照，第二轮才反映本轮进入重连的客户端
        monitor._check_all_clients()
        assert monitor._client_stats["reconnecting"] == 1
        assert monitor._client_stats["orphan_streams"] == 0

    def test_no_reconnect_when_decoder_alive_even_if_frames_stale(self):
        """进程活着但帧陈旧（等首帧/瞬时停）→ 只等，不进重连（这是启动 bug 的根治点）。"""
        client_id = "monitor_alive_stale"
        # 帧已 10s 没更新（旧判据会误杀），但进程活着
        mock_cq = self._cq(seconds_ago=10.0)

        monitor = _make_monitor(
            client_id, mock_cq, active_decoder_ids={client_id}, decoder_alive=True
        )

        monitor._check_all_clients()
        assert client_id not in monitor._reconnecting_clients, (
            "进程活着时即便帧陈旧也不应进入重连（等首帧不能被杀）"
        )
        # 未超 cleanup_timeout(20s)，也不应清理
        monitor._stream_service.restart_stream.assert_not_called()

    def test_enters_orphan_when_decoder_not_registered(self):
        """decoder 未注册（has_decoder=False）→ 走 orphan 路径，不进重连。"""
        client_id = "monitor_orphan_test"
        mock_cq = self._cq(seconds_ago=10.0)

        monitor = _make_monitor(
            client_id, mock_cq, active_decoder_ids=set()
        )

        monitor._check_all_clients()
        assert client_id not in monitor._reconnecting_clients
        assert monitor._client_stats["orphan_streams"] == 1


# ===========================================================================
# Part 3：完整状态机场景
# ===========================================================================

class TestFullReconnectScenario:
    """端到端：进程死 → respawn → 来帧退出重连；以及无帧超时 → cleanup。"""

    def test_full_reconnect_state_machine(self):
        """
        Round 1: 进程死 → 进入重连模式
        Round 2: 仍死 + 到节流窗（last_attempt_time=0）→ 触发 restart_stream()
        Round 3: 来了新帧（frame_age < 阈值）→ 退出重连模式
        """
        client_id = "full_scenario_client"
        base_timestamp = time.time() - 10.0

        mock_cq = MagicMock()
        mock_cq.latest_raw_timestamp = base_timestamp
        mock_cq.task_started_at = 0.0

        monitor = _make_monitor(
            client_id, mock_cq, active_decoder_ids={client_id}, decoder_alive=False
        )

        # Round 1：进程死 → 进入重连
        monitor._check_all_clients()
        assert client_id in monitor._reconnecting_clients, "Round 1: 应进入重连模式"

        # Round 2：仍死 + 节流窗到 → respawn
        monitor._check_all_clients()
        assert monitor._stream_service.restart_stream.called, (
            "Round 2: 应已调用 restart_stream() 做 respawn"
        )

        # 模拟推流端就绪，新帧到来
        mock_cq.latest_raw_timestamp = time.time()

        # Round 3：来了新帧 → 退出重连
        monitor._check_all_clients()
        assert client_id not in monitor._reconnecting_clients, (
            "Round 3: 有新帧后应退出重连模式"
        )

    def test_gives_up_after_cleanup_timeout(self):
        """无帧时长 ≥ cleanup_timeout（=20s）→ cleanup（纯时间触发，不数次数）。"""
        client_id = "giveup_client"
        # 帧已 25s 没更新（> cleanup_timeout 20s）
        base_timestamp = time.time() - 25.0

        mock_cq = MagicMock()
        mock_cq.latest_raw_timestamp = base_timestamp
        mock_cq.task_started_at = 0.0

        monitor = _make_monitor(
            client_id, mock_cq, active_decoder_ids={client_id}, decoder_alive=False
        )
        monitor._stream_service.restart_stream.return_value = False  # respawn 始终失败

        # Round 1：进程死 → 进入重连
        monitor._check_all_clients()
        assert client_id in monitor._reconnecting_clients

        # Round 2：_handle 中 idle(25s) >= cleanup_timeout(20s) → FAILED → cleanup → 退出
        monitor._check_all_clients()
        assert client_id not in monitor._reconnecting_clients, (
            "无帧超 cleanup_timeout 后应退出重连模式（执行 cleanup）"
        )


class TestReconnectIdentityFence:
    """进入重连捕获 cq_A；槽位被 /start 换成新 run 时，重连按对象身份放弃。"""

    def _cq(self, seconds_ago: float = 10.0) -> MagicMock:
        cq = MagicMock()
        cq.latest_raw_timestamp = time.time() - seconds_ago
        cq.task_started_at = 0.0
        return cq

    def test_reconnect_captures_cq_on_entry(self):
        """进入重连时把当前槽位 cq 存进 ReconnectState.cq（fence 基准）。"""
        client_id = "fence_capture"
        cq_a = self._cq()
        monitor = _make_monitor(
            client_id, cq_a, active_decoder_ids={client_id}, decoder_alive=False
        )

        monitor._check_all_clients()
        assert monitor._reconnecting_clients[client_id].cq is cq_a

    def test_reconnect_abandoned_when_slot_replaced(self):
        """次轮槽位已换成新 cq_B → 放弃本次重连，不误动新 run。"""
        client_id = "fence_swap"
        cq_a = self._cq()
        monitor = _make_monitor(
            client_id, cq_a, active_decoder_ids={client_id}, decoder_alive=False
        )

        # Round 1：进入重连，捕获 cq_A
        monitor._check_all_clients()
        assert client_id in monitor._reconnecting_clients

        # 模拟 /start 抢占重启：槽位换成全新 cq_B
        cq_b = self._cq()
        monitor._client_manager.snapshot.return_value = {client_id: cq_b}

        # Round 2：当前 cq(cq_B) 非捕获的 cq_A → 放弃重连，且不对新 run 发起 restart
        monitor._stream_service.restart_stream.reset_mock()
        monitor._check_all_clients()

        assert client_id not in monitor._reconnecting_clients
        monitor._stream_service.restart_stream.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
