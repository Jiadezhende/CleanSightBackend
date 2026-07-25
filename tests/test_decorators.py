"""日志装饰器：client_id 提取优先级 / 参数清洗 / log_call·timing 行为透明性。

装饰器只做日志，不改函数语义 —— 核心断言是"透明"：返回值原样、异常照抛。
"""

import logging
import time
from types import SimpleNamespace

import numpy as np

from app.utils import context
from app.utils.decorators import (
    _extract_client_id,
    _sanitize_args,
    log_call,
    timing,
)


# ---- _extract_client_id 提取优先级（kwargs > args[0] str > self.client_id > 上下文）----

def test_extract_from_kwargs():
    assert _extract_client_id((), {"client_id": "kw"}) == "kw"


def test_extract_from_args0_str():
    assert _extract_client_id(("10.0.0.1", 42), {}) == "10.0.0.1"


def test_extract_from_self_attr():
    obj = SimpleNamespace(client_id="self-id")
    assert _extract_client_id((obj,), {}) == "self-id"


def test_extract_fallback_to_context():
    context.clear_context()
    context.set_client_id("ctx-id")
    try:
        # args[0] 非 str、无 client_id 属性 → 回退线程上下文
        assert _extract_client_id((123,), {}) == "ctx-id"
    finally:
        context.clear_context()


def test_extract_none_when_nothing():
    context.clear_context()
    assert _extract_client_id((123,), {}) is None


# ---- _sanitize_args 清洗大对象 ----

def test_sanitize_ndarray_to_shape_repr():
    out = _sanitize_args((np.zeros((4, 4, 3), dtype=np.uint8),), {})
    assert "ndarray shape=(4, 4, 3)" in out["args"][0]


def test_sanitize_bytes_to_len():
    out = _sanitize_args((b"abcde",), {})
    assert out["args"][0] == "<bytes len=5>"


def test_sanitize_small_value_passthrough():
    out = _sanitize_args(("hi", 7), {"k": "v"})
    assert out["args"] == ("hi", 7)
    assert out["kwargs"] == {"k": "v"}


# ---- log_call 透明性 ----

def test_log_call_returns_result():
    @log_call()
    def add(a, b):
        return a + b

    assert add(2, 3) == 5


def test_log_call_propagates_exception():
    @log_call()
    def boom():
        raise KeyError("x")

    try:
        boom()
        assert False, "should have raised"
    except KeyError:
        pass


def test_log_call_skip_in_production_returns_original(monkeypatch):
    # 非 debug + skip_in_production → 直接返回原函数（零包装开销）
    monkeypatch.setattr("app.utils.decorators._is_debug_mode", lambda: False)

    def raw():
        return "r"

    wrapped = log_call(skip_in_production=True)(raw)
    assert wrapped is raw


def test_log_call_logs_enter_exit(caplog):
    @log_call(level=logging.INFO)
    def work():
        return 1

    with caplog.at_level(logging.INFO):
        work()
    text = caplog.text
    assert "[ENTER]" in text and "[EXIT]" in text


# ---- timing 透明性 + 阈值告警 ----

def test_timing_returns_result():
    @timing()
    def mul(a, b):
        return a * b

    assert mul(3, 4) == 12


def test_timing_warns_when_over_threshold(caplog):
    # 睡 5ms、阈值 0.1ms → 必超 → 一条 [SLOW] warning（threshold=0 会被 `if threshold_ms` 判假，故用正阈值）
    @timing(threshold_ms=0.1, warn_on_slow=True)
    def slow():
        time.sleep(0.005)

    with caplog.at_level(logging.WARNING):
        slow()
    assert "[SLOW]" in caplog.text
