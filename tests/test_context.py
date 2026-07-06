"""线程本地上下文（client_id / task_id）set/get/clear + ClientContext 嵌套还原。

纯 threading.local，无 I/O。用例间必须清干净（同线程跑，状态会渗）。
"""

import pytest

from app.utils import context


@pytest.fixture(autouse=True)
def _clean_context():
    context.clear_context()
    yield
    context.clear_context()


def test_get_returns_none_when_unset():
    assert context.get_client_id() is None
    assert context.get_task_id() is None


def test_client_id_set_get_clear():
    context.set_client_id("10.0.0.1")
    assert context.get_client_id() == "10.0.0.1"
    context.clear_client_id()
    assert context.get_client_id() is None


def test_task_id_set_get_clear():
    context.set_task_id(7)
    assert context.get_task_id() == 7
    context.clear_task_id()
    assert context.get_task_id() is None


def test_clear_context_clears_both():
    context.set_client_id("c")
    context.set_task_id(1)
    context.clear_context()
    assert context.get_client_id() is None
    assert context.get_task_id() is None


def test_clear_is_idempotent_when_unset():
    # 未设置时清理不抛（hasattr 守卫）
    context.clear_client_id()
    context.clear_task_id()
    assert context.get_client_id() is None


def test_client_context_sets_within_and_restores_to_none():
    with context.ClientContext(client_id="1.2.3.4", task_id=9):
        assert context.get_client_id() == "1.2.3.4"
        assert context.get_task_id() == 9
    # 退出还原为进入前（此处为未设置 → None）
    assert context.get_client_id() is None
    assert context.get_task_id() is None


def test_client_context_nesting_restores_outer():
    with context.ClientContext(client_id="outer", task_id=1):
        with context.ClientContext(client_id="inner", task_id=2):
            assert context.get_client_id() == "inner"
            assert context.get_task_id() == 2
        # 内层退出 → 还原外层
        assert context.get_client_id() == "outer"
        assert context.get_task_id() == 1


def test_client_context_does_not_suppress_exception():
    with pytest.raises(ValueError):
        with context.ClientContext(client_id="c", task_id=1):
            raise ValueError("boom")
    # 异常路径也还原
    assert context.get_client_id() is None
