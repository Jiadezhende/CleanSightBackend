"""离线策略目录：impl/ 下每个策略包的 segmenter.py docstring 都带一段可直接粘贴进 stage 的 YAML，且能按它装配出来。

研究时切换历史策略靠复制这段 YAML，所以它必须是合法 YAML、class 路径可 import、参数与构造签名对得上、物料落在
「一个模型一个目录」下。只构造不前向，不加载权重。
"""

from pathlib import Path

import pytest
import yaml

from app.services.inference.config import InferenceConfig, _expand_env_vars
from app.services.inference.stage_factory import StageFactory

IMPL = Path(__file__).resolve().parents[1] / "app" / "services" / "inference" / "offline" / "impl"
PACKAGES = sorted(p.name for p in IMPL.iterdir() if p.is_dir() and (p / "segmenter.py").is_file())


def _snippet(pkg):
    import importlib

    doc = importlib.import_module(f"app.services.inference.offline.impl.{pkg}.segmenter").__doc__
    block = "offline:" + doc.split("offline:", 1)[1].split("\n\n", 1)[0]
    return _expand_env_vars(yaml.safe_load(block))["offline"]


def test_every_strategy_package_is_listed():
    assert PACKAGES == [
        "clean_asformer", "clean_bigru", "clean_gru_nodep", "clean_mstcn2_bundle", "clean_mstcn2_role_embed",
        "clean_mstcn_bilstm",
    ]


@pytest.mark.parametrize("pkg", PACKAGES)
def test_docstring_yaml_builds_its_own_strategy(pkg):
    offline = _snippet(pkg)
    assert offline["class"].startswith(f"app.services.inference.offline.impl.{pkg}.segmenter.")
    seg = StageFactory(InferenceConfig({"stages": {"2": {"offline": offline}}})).create_offline_segmenter("2")
    assert type(seg).__name__ == offline["class"].rsplit(".", 1)[1]
    model_dir = Path(offline["params"]["model_dir"])
    assert model_dir.name == "clean-offline-" + pkg.removeprefix("clean_").replace("_", "-")
    assert seg.materials() and all(Path(p).parent in (model_dir, Path(offline["params"].get("backbone_dir", model_dir)))
                                   for p in seg.materials())
