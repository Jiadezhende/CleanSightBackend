"""
tests/test_stream_rewrite.py

测试 _rewrite_rtsp_url：后端拉流时将代理端口替换为 MediaMTX 内部端口。
"""

import pytest

from app.services.stream.service import _rewrite_rtsp_url

PROXY = 8004
INTERNAL = 18004


# 端口匹配 proxy_port 即说明目标是本机 MediaMTX，host 一律改写为 127.0.0.1
# （覆盖 localhost / 外网 IP / IPv6 / 任意 host，避免流量绕道外网网卡被 iptables 拦截）
@pytest.mark.parametrize("url, expected", [
    # 端口匹配 → host 统一改写为 127.0.0.1
    ("rtsp://127.0.0.1:8004/cam1",          "rtsp://127.0.0.1:18004/cam1"),
    ("rtsp://192.168.1.100:8004/stream",    "rtsp://127.0.0.1:18004/stream"),
    ("rtsp://user:pass@127.0.0.1:8004/cam", "rtsp://user:pass@127.0.0.1:18004/cam"),
    # 凭据保留，host 改写
    ("rtsp://user:pass@192.168.1.100:8004/cam", "rtsp://user:pass@127.0.0.1:18004/cam"),
    ("rtsp://localhost:8004/live/test",     "rtsp://127.0.0.1:18004/live/test"),
    # 显式 IPv6 literal 端口匹配 → 同样改写为 127.0.0.1
    ("rtsp://[::1]:8004/live/x",             "rtsp://127.0.0.1:18004/live/x"),
    # 端口不匹配 → 原样返回（host 不动）
    ("rtsp://[::1]:554/live/x",              "rtsp://[::1]:554/live/x"),
    ("rtsp://127.0.0.1:554/cam1",           "rtsp://127.0.0.1:554/cam1"),
    ("rtsp://camera.local:9000/stream",     "rtsp://camera.local:9000/stream"),
    # 无端口 → 原样返回
    ("rtsp://127.0.0.1/cam1",              "rtsp://127.0.0.1/cam1"),
])
def test_rewrite_rtsp_url(url, expected):
    assert _rewrite_rtsp_url(url, PROXY, INTERNAL) == expected
