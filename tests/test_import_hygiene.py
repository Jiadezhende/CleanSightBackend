"""导入纪律门禁（规范 §7：唯一硬指标）

两条：

1. **导入预算**：目标模块在干净子进程里 import 后，`sys.modules` 不得含预算外的重依赖，
   且耗时不超上限。守住「重依赖懒加载」这条从未被检查过的既有意图——它此前失守两次
   （`app.main` 拽 torch、`persistence.*` 拽 cv2），都是模块级构造/re-export 悄悄引入的。
2. **单例引用面**：服务单例只许被 `run_control`（编排中枢）/ `routers/*`（装配层）/
   本包 `lifespan()` import。同时守住 `docs/DEVELOPMENT.md` §3 写下但无人检查的
   「不建 service 对 service 的直接依赖」。

规范全文：`docs/update/20260903_PACKAGE_LAYOUT_SPEC.md`。
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"

# 被盯防的重依赖。numpy/sqlalchemy 不在此列——它们按模块各自入白名单放行。
HEAVY = ("torch", "ultralytics", "cv2")

# (模块, 允许出现的重依赖集合, 耗时上限秒)。上限取实测 ~3-5× 余量，只兜「量级失守」，
# 不做性能回归——机器负载下 import 抖动大，卡太紧会变成噪声源。
BUDGET = {
    "app.domain":               (set(), 0.20),
    "app.services.client":      (set(), 1.0),
    "app.services.inference":   (set(), 1.0),
    "app.services.persistence": (set(), 1.0),
    "app.main":                 (set(), 2.0),
}

# 服务单例 → 定义它的模块。client_manager **不在此列**：它是零跨服务依赖的中台 leaf，
# 谁都可以向下依赖它（见 docs/kb 的 client 中台约定），限制它的引用面没有意义。
SINGLETONS = {
    "stream_service": "app.services.stream.instance",
    "inference_manager": "app.services.inference.instance",
    "persistence_manager": "app.services.persistence.instance",
    "health_monitor": "app.services.health_monitor.instance",
    "run_controller": "app.services.run_control",
}

# 允许 import 单例的文件（相对 REPO_ROOT）。前三类由规则表达（见 _is_allowed_importer），
# 这里只列**具名例外**——每条都得有理由，加新的先想清楚为什么不能走 run_control。
SINGLETON_EXCEPTIONS = {
    # 健康监控是与 run_control 并列的自动化协调者：它按秒轮询各服务状态并发起重连/清理，
    # 天然要持三个协作者。三处 import 均写在 `_resolve_deps()` 函数体内（不是模块级），
    # 且 run_controller 那处是反向指回编排中枢做拆除。
    "app/services/health_monitor/manager.py",
    # 告警落库 sink：inference 产告警 → persistence 落库。跨服务但方向正确（下游依赖），
    # 且 sink 就是为这条方向存在的唯一窄接口。
    "app/services/inference/temporal/alarm_sink.py",
}


def _import_in_subprocess(module: str):
    """在干净子进程里 import，返回 (耗时秒, 已加载的重依赖列表)。

    必须起子进程：pytest 进程早已把 torch/cv2 装进 `sys.modules`（别的用例导过），
    在本进程里测等于测了个寂寞。
    """
    code = (
        "import json, sys, time\n"
        "t = time.perf_counter()\n"
        f"__import__({module!r})\n"
        "elapsed = time.perf_counter() - t\n"
        f"heavy = [m for m in {HEAVY!r} if m in sys.modules]\n"
        "print(json.dumps({'elapsed': elapsed, 'heavy': heavy}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"import {module} 失败：\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", sorted(BUDGET))
def test_import_budget(module):
    allowed, max_seconds = BUDGET[module]
    result = _import_in_subprocess(module)

    leaked = set(result["heavy"]) - allowed
    assert not leaked, (
        f"`import {module}` 拽入了预算外的重依赖 {sorted(leaked)}。"
        f"排查：模块级构造（单例、re-export）或顶层 import 把 L2 依赖提前拉起了。"
        f"三条合法通路见规范 §2：impl/ 经 importlib 加载、函数体内 import、workers/ 子进程。"
    )
    assert result["elapsed"] < max_seconds, (
        f"`import {module}` 耗时 {result['elapsed']:.2f}s，超上限 {max_seconds}s。"
        f"通常意味着有重活跑在了 import 期（应推迟到 start()）。"
    )


def _iter_app_py_files():
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _is_allowed_importer(rel: str) -> bool:
    """规范 §6 的引用面三类 + 具名例外。"""
    if rel in SINGLETON_EXCEPTIONS:
        return True
    if rel == "app/services/run_control.py":        # 编排中枢
        return True
    if rel.startswith("app/routers/"):              # 装配层
        return True
    if rel.endswith("/__init__.py"):                # 本包 lifespan()（在函数体内 import）
        return True
    return False


def test_singleton_reference_surface():
    """服务单例只许被编排中枢 / 装配层 / 本包 lifespan() import。"""
    violations = []

    for path in _iter_app_py_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if _is_allowed_importer(rel):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            for alias in node.names:
                owner = SINGLETONS.get(alias.name)
                if owner is not None and node.module == owner:
                    violations.append(f"{rel}:{node.lineno} → {alias.name}")

    assert not violations, (
        "以下文件直接 import 了服务单例，违反规范 §6 的引用面：\n  "
        + "\n  ".join(violations)
        + "\n服务间协作应经 run_control 编排；确有正当理由的加进 SINGLETON_EXCEPTIONS 并写明。"
    )


def test_services_do_not_import_routers():
    """services 不得反向依赖 routers（分层里唯一出现过的真环，已在期 1 消掉）。"""
    violations = []
    for path in sorted((APP_DIR / "services").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.routers"):
                violations.append(f"{rel}:{node.lineno} → {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.routers"):
                        violations.append(f"{rel}:{node.lineno} → {alias.name}")

    assert not violations, (
        "services 反向依赖了 routers（协议层）：\n  " + "\n  ".join(violations)
    )
