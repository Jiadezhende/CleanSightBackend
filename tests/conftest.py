"""共享 fixture：包装 factories 的 factory-as-fixture + 真正跨文件共享的 setup。

Hybrid 约定：
- 主用法是直接 `from factories import make_cq`（可 @parametrize、可被 integration_tests 复用）；
- 需要注入式书写的用例，用下方 factory-fixture：`def test_x(make_cq): cq = make_cq(task_id=2)`。
两者同源（都指向 factories 里的纯函数），不产生第二份构造逻辑。
"""

import pytest

import factories
from app.settings import settings


# ---- factory-as-fixture：返回可调用的构造器（支持 override 参数）----

@pytest.fixture
def make_cq():
    return factories.make_cq


@pytest.fixture
def make_detection():
    return factories.make_detection


@pytest.fixture
def make_frame_inference():
    return factories.make_frame_inference


# ---- 真正跨文件共享的 setup ----

@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """把 settings.storage_dir 指到隔离临时目录，读写两侧同源、用例间不串。

    收编 test_traceback_segment_finder 等处散落的 monkeypatch.setattr(settings, ...)。
    """
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    return tmp_path
