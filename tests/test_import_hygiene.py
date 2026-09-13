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
    # storage 的每个模块**逐个登记**，不能只登记包名：包根是标记型 __init__、零 re-export，
    # `import app.storage` 根本不加载任何域文件（实测 1ms / 41 模块），登记包名挡不住有人
    # 往域文件里塞 ffmpeg/cv2/批缓冲。新增域文件必须同时在这里加一行 ——
    # 由 test_layer_package_modules_are_all_budgeted 强制。
    #
    # 重域用子包（`hls/`）时，它的 `__init__` 是 **facade 不是标记型**：re-export 会连带
    # 加载 `_encode` 之类的实现模块，所以那些模块的模块级必须保持 stdlib-only（cv2 走函数
    # 体内 import），`app.storage.hls` 这条才能维持「重依赖集合为空」。
    #
    # ⚠ **子包成员条目实际量的是整个 facade，不是它自己**：import 任何
    # `app.storage.hls.X` 都会先跑包 `__init__`，于是 `_layout` / `_m3u8` / `_read` /
    # `types` 这几条的实测值与 `app.storage.hls` 一模一样（~0.10s，且 numpy 已加载）。
    # 后面标着「stdlib only」的注释说的是**源码事实**，不是本门禁的结论 ——
    # 往 `types.py` 里塞一行 `import numpy` 不会让任何一条红（numpy 不在 HEAVY，
    # 且 facade 早已把它拉起来了）。真正被这些条目守住的只有 HEAVY 三项：实测往
    # `types.py` 塞 `import cv2`，`types` 与 `_read` 两条会一起红。
    # 要让「某个成员模块自己是不是 stdlib-only」可执行，得另起一条按源码 AST 查
    # import 的检查，不是调这里的秒数。
    "app.storage":              (set(), 0.20),
    "app.storage._root":        (set(), 0.20),   # stdlib only
    "app.storage.tasks":        (set(), 0.20),   # stdlib only
    # feature 出 FrameFeature → 吃 app.domain（numpy 随 Detection.mask 的标注进来）。
    # 这是 D1 允许的唯一一档 L1 依赖，上限按 app.domain 的量级加余量。
    "app.storage.feature":      (set(), 0.40),
    # hls 是子包，facade `__init__` 会连带加载下面每个实现模块 —— 所以 `app.storage.hls`
    # 这条盯的是**整个域**的模块级依赖。cv2 必须留在 `_encode.write_mp4v` 的函数体内，
    # 塞回模块级会让这条连同 `app.storage.hls._encode` 一起红。
    "app.storage.hls":          (set(), 0.40),   # 域货币 Frame → app.domain（numpy）
    "app.storage.hls._encode":  (set(), 0.40),   # 同上；cv2 在函数体内
    # 解码侧：货币是 Frame + sidecar 的 float64 数组，故吃 app.domain + numpy。ffmpeg 是
    # **运行时**依赖（D5），import 时不该出现任何重依赖 —— 尤其不该有 cv2：解码走 ffmpeg
    # 管道，一旦有人图省事换成 cv2.VideoCapture，这条会连同 `app.storage.hls` 一起红。
    "app.storage.hls._decode":  (set(), 0.40),
    # 下面标着「stdlib only」的四条（`_fmp4` / `_layout` / `_m3u8` / `_meta`）秒数上限
    # 照 `app.storage.hls` 给 0.40 —— 它们量的是同一份活（见上方 ⚠ 段），给 0.20 只会让
    # 负载高的机器上这几条先于 facade 那条抖。`_read` / `types` 同理。
    "app.storage.hls._fmp4":    (set(), 0.40),   # stdlib only（ffmpeg 是运行时依赖，D5）
    "app.storage.hls._idx":     (set(), 0.40),   # numpy 是它的货币（float64 数组）
    "app.storage.hls._write":   (set(), 0.40),   # 域货币 Frame
    "app.storage.hls._layout":  (set(), 0.40),   # stdlib only
    "app.storage.hls._m3u8":    (set(), 0.40),   # stdlib only
    "app.storage.hls._meta":    (set(), 0.40),   # stdlib only
    # 读侧组合动作（可播段过滤 / 段级区间定位）与资源容器。两者的源码都是 stdlib-only
    # （`_read` 只组合 `_layout` + `_m3u8`；`types` 是子包的底、不 import 同包任何模块），
    # 但**本门禁验不到这一点** —— 见上方 BUDGET 开头的 ⚠ 段。
    "app.storage.hls._read":    (set(), 0.40),
    "app.storage.hls.types":    (set(), 0.40),
    # 服务层工具包。标记型 __init__（零 re-export），故这条盯的只是它自己；每个成员模块
    # 另行登记，由 test_layer_package_modules_are_all_budgeted 强制。
    "app.services.utils":              (set(), 0.20),
    "app.services.utils.vod_playlist": (set(), 0.20),   # stdlib only（math / typing）
    "app.services.client":      (set(), 1.0),
    "app.services.inference":   (set(), 1.0),
    "app.services.persistence": (set(), 1.0),
    # recording 登记两条：包名那条是门面型（浅，基本只有 docstring），真正的守门人是
    # `service` —— 它 import `app.storage.hls`，cv2 一旦从 `_encode` 的函数体挪到模块级，
    # 这条会先红。
    "app.services.recording":         (set(), 1.0),
    "app.services.recording.service": (set(), 1.0),
    "app.main":                 (set(), 2.0),
}

# 分层包 → 它允许 import 的 `app.*` 前缀白名单（包内互相 import 由 self 前缀覆盖）。
#
# **白名单而非黑名单**：`app/storage` 是 services 下面一层的数据层，它能被写侧
# （persistence）与读侧（traceback / lab / inference.offline / routers）同时依赖的前提，
# 是它谁都不依赖。旧规则只黑名单了 `app.services.*`，挡不住 `app.database` / `app.models`
# ——那两个一进来，数据层就绑死了 ORM，而这不会造环、不会红，只会在某天想换存储时才发现。
LAYER_PACKAGES = {
    # app.domain：内存数据契约（Frame / FrameFeature），本层的入参出参就是它们
    # app.settings：落盘根的唯一来源，按 `_root.py` 的规矩只在函数体内 import
    "app/storage": ("app.storage", "app.domain", "app.settings"),
    # 服务层工具：多个 service / router 都要、但不属于任何一个的无状态纯函数。它可以向下
    # 依赖数据层与基建，但**不得 import 任何兄弟 service 包** —— 破了它，本包就成了
    # service → service 依赖的后门：lab 想调 traceback 的东西，在这里加个转发函数就绕过去
    # 了，而 test_singleton_reference_surface 只盯单例、看不见这种转发。
    #
    # 注意 "app.services.utils" 作为白名单前缀**不会**放行 "app.services.lab"：检查是
    # `name == ok or name.startswith(ok + ".")`，兄弟包差的正是那个点。
    "app/services/utils": (
        "app.services.utils", "app.storage", "app.domain", "app.utils", "app.settings",
    ),
}

# 服务单例 → 定义它的模块。client_manager **不在此列**：它是零跨服务依赖的中台 leaf，
# 谁都可以向下依赖它（见 docs/kb 的 client 中台约定），限制它的引用面没有意义。
SINGLETONS = {
    "stream_service": "app.services.stream.instance",
    "inference_manager": "app.services.inference.instance",
    "persistence_manager": "app.services.persistence.instance",
    "recording_service": "app.services.recording.instance",
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


@pytest.mark.parametrize("package", sorted(LAYER_PACKAGES))
def test_layer_package_modules_are_all_budgeted(package):
    """分层包里每个模块都得有自己的 BUDGET 条目 —— 否则新域文件天生不在门禁视野里。

    补的是 20260909 记录里点名的那个洞：BUDGET 登记包名，而标记型 `__init__` 不加载任何
    域文件，于是「往包里塞 cv2 会先红」并不成立。逐模块登记 + 本条覆盖检查才成立。

    模块名按目录层级拼，重域用子包（`hls/_m3u8.py` → `app.storage.hls._m3u8`）时同样成立；
    各级 `__init__` 折叠成所在包本身。
    """
    package_module = package.replace("/", ".")
    expected = set()
    for path in sorted((REPO_ROOT / package).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT / package).with_suffix("")
        parts = [part for part in rel.parts if part != "__init__"]
        expected.add(".".join([package_module, *parts]))

    missing = sorted(expected - set(BUDGET))
    assert not missing, (
        f"分层包 {package} 里这些模块没有导入预算：{missing}\n"
        "在 BUDGET 里加一行并写明允许哪些重依赖——新增域文件时这是一次显式决策。"
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


@pytest.mark.parametrize("package", sorted(LAYER_PACKAGES))
def test_layer_package_imports_only_whitelisted_app_modules(package):
    """分层包只许 import 白名单里的 `app.*`（stdlib 与三方不受限）。

    `app/storage` 是 services 下面一层的数据层。它一旦向上或向旁伸手，那一头就不能再
    依赖它——而写侧与读侧同时依赖它正是抽这个包的全部意义。白名单比黑名单严一档：
    `app.database` / `app.models` 进来不会造环、不会红，只会把数据层绑死在 ORM 上。
    """
    allowed = LAYER_PACKAGES[package]
    violations = []

    for path in sorted((REPO_ROOT / package).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # 相对 import（level > 0）是包内寻址，天然合规
                names = [] if node.level else [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                if not name.startswith("app."):
                    continue
                if any(name == ok or name.startswith(ok + ".") for ok in allowed):
                    continue
                violations.append(f"{rel}:{node.lineno} → {name}")

    assert not violations, (
        f"分层包 {package} import 了白名单外的 app 模块，它就不再是所有人的共同下游：\n  "
        + "\n  ".join(violations)
        + f"\n白名单：{list(allowed)}。确有正当理由的，改 LAYER_PACKAGES 并在包 docstring 的"
        "边界声明里写明为什么它属于这一层。"
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
