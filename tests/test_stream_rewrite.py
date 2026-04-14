"""
tests/test_stream_rewrite.py

测试 _rewrite_rtsp_url：后端拉流时将代理端口替换为 MediaMTX 内部端口。
"""

import pytest

from app.services.stream.service import _rewrite_rtsp_url

PROXY = 8004
INTERNAL = 18004


@pytest.mark.parametrize("url, expected", [
    # 需要重写的情况
    ("rtsp://127.0.0.1:8004/cam1",          "rtsp://127.0.0.1:18004/cam1"),
    ("rtsp://192.168.1.100:8004/stream",    "rtsp://192.168.1.100:18004/stream"),
    ("rtsp://user:pass@127.0.0.1:8004/cam", "rtsp://user:pass@127.0.0.1:18004/cam"),
    # Windows 上 localhost 解析为 ::1（IPv6），重写时强制转为 127.0.0.1
    ("rtsp://localhost:8004/live/test",     "rtsp://127.0.0.1:18004/live/test"),
    # 显式 IPv6 literal → netloc 需保留方括号
    ("rtsp://[::1]:8004/live/x",             "rtsp://[::1]:18004/live/x"),
    ("rtsp://[::1]:554/live/x",              "rtsp://[::1]:554/live/x"),
    # 端口不匹配 → 原样返回
    ("rtsp://127.0.0.1:554/cam1",           "rtsp://127.0.0.1:554/cam1"),
    ("rtsp://camera.local:9000/stream",     "rtsp://camera.local:9000/stream"),
    # 无端口 → 原样返回
    ("rtsp://127.0.0.1/cam1",              "rtsp://127.0.0.1/cam1"),
])
def test_rewrite_rtsp_url(url, expected):
    assert _rewrite_rtsp_url(url, PROXY, INTERNAL) == expected
