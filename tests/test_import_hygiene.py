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
    # step_store 此前实测 0.3s+，现在与空跑 `import app.services` 同量级（热 __pycache__
    # 下 ~0.001s）：差额全是 `app/services/__init__.py` 顶层 `from .client import
    # client_manager` 这笔过路费（零消费方，已删），删掉后 step_store 也不再顺带把
    # settings 拉进 sys.modules。上限仍取 1.0 与其他服务包一致——真正盯的是重依赖
    # 集合为空那条，耗时只兜量级失守。
    "app.services.step_store":  (set(), 1.0),
    "app.services.client":      (set(), 1.0),
    "app.services.inference":   (set(), 1.0),
    "app.services.persistence": (set(), 1.0),
    "app.main":                 (set(), 2.0),
}

# `step_store` 是零跨服务依赖的 leaf：写侧 persistence 与读侧 traceback / lab /
# inference.offline 都向它依赖，故它一旦回指任何 app.services.* 就会成环——那正是
# 抽出本包要消掉的东西（此前写侧不敢依赖读侧，只好把格式知识再写一遍，重复由此而来）。
LEAF_PACKAGE = "app/services/step_store"

# 存储根的可见范围。**根拿不到，路径就无从拼起** —— 这比「数还有几处 `/` 拼接」
# 可查得多：目录 = 根 + 两级 id，只要没人能拿到根，落盘布局就只能问 step_store。
STORAGE_ROOT_ATTR = "storage_base_dir"
STORAGE_ROOT_ALLOWED = ("app/settings.py", f"{LEAF_PACKAGE}/")
# 拦属性访问还不够：`store._storage_root()` 返回的就是那个根，import 一下就绕过去了。
# 两条一起拦才叫「根拿不到」。
STORAGE_ROOT_FN = "_storage_root"

# 写成员的调用者白名单 = 往 step 目录里写东西的三个服务模块。计划外的第四个写者要先在
# 这里登记——**这条门禁是「谁在写」的唯一约束**（TTL 可见性由写入口顺带刷的活动标记保证，
# 与写者是谁无关，没有注册表可以顺带拦一道）。
#
# 名字都挑了有区分度的：**别把 `open` 也列进来**——AST 按属性名匹配，会把满仓的
# `path.open(...)` 全部误报。
WRITE_MEMBERS = (
    "segment_path", "sidecar_path", "init_path", "playlist_path", "metadata_path",
    "file_path", "open_file",
)
WRITE_MEMBER_ALLOWED = (
    "app/services/persistence/strategies/hls_strategy.py",
    "app/services/inference/feature/store.py",
    "app/services/inference/offline/runner.py",
    f"{LEAF_PACKAGE}/",
)

# 下划线开头的三个模块是包内私有：对外只有 `store`（目录契约）与 `hls`（视频落盘域）
# 两个模块，「内部实现对外全部不可见」。破这条即意味着调用方又开始自己拼名字/骨架，
# 而备料（EXTINF 真值、滤在途、判 init、算 TARGETDURATION）写错是**静默**的。
PRIVATE_MODULES = {
    "app.services.step_store._layout",
    "app.services.step_store._playlist",
    "app.services.step_store._decoder",
}
# 具名例外，各有退出条件。key = 模块名，value = {允许的文件}
PRIVATE_MODULE_ALLOWED = {
    "app.services.step_store._playlist": {
        # 写侧是 playlist 格式的**定义者**（手写 EXTINF 行与文件头、算 tfdt 前缀和），
        # 不是消费者。**退出条件**：写侧事务化（`hls.write_segment`）后，首行与 EXTINF
        # append 都进包内，这条随之删掉。
        "app/services/persistence/strategies/hls_strategy.py",
        # 每段 EXTINF 取相邻段 ts 跨度而非 playlist EXTINF（seek 基准是 ts，换了会逐段
        # 错位），故走不了 `hls.vod_playlist`。**退出条件**：验证两者在 fps 漂移下等价
        # 后改走成品出口，然后删掉本行。
        "app/services/lab/clip_builder.py",
    },
    "app.services.step_store._layout": {
        # 同上两条的连带：它们手拼 m3u8 首行时要 `init_name` 写 EXT-X-MAP。
        "app/services/persistence/strategies/hls_strategy.py",
        "app/services/lab/clip_builder.py",
    },
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


def test_step_store_is_a_leaf():
    """step_store 不得 import 任何其他 app.services.* 包（含 app.routers）。

    它只允许依赖 stdlib、numpy 与 `app.settings`（`_storage_root` 读存储根，
    写在函数体内）。破这条即意味着 leaf 地位失守，写侧读侧的循环依赖会重新长出来。
    """
    leaf_dir = REPO_ROOT / LEAF_PACKAGE
    violations = []

    for path in sorted(leaf_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                # 本包内部互相 import 是允许的（layout ← finder 等）
                if name.startswith(f"{LEAF_PACKAGE.replace('/', '.')}"):
                    continue
                if name.startswith("app.services.") or name.startswith("app.routers"):
                    violations.append(f"{rel}:{node.lineno} → {name}")

    assert not violations, (
        "step_store 不再是 leaf —— 它 import 了别的服务包：\n  "
        + "\n  ".join(violations)
        + "\n本包是写侧读侧的公共下游，回指任何服务包都会重新造出循环依赖。"
    )


def _iter_app_trees():
    """(相对路径, AST) 逐个产出 `app/` 下的 .py。"""
    for path in _iter_app_py_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_storage_root_is_private_to_step_store():
    """除 `app/settings.py` 与 step_store 外，谁都不许拿到存储根。

    两个口子都要堵：`settings.storage_base_dir` 这个属性，以及包内私有的
    `store._storage_root()` —— 后者返回的就是同一个值，只拦属性访问等于没拦。

    存储根一旦流出去，调用方就能自己拼「根 + task_id + step_id + 文件名」，落盘布局
    重新复制一份到各处——那正是抽出本包要消掉的东西，且**这种拼接门禁抓不到**
    （`root / "x"` 是普通 Path 拼接，看不出它在拼 step 目录）。故拦在源头：拦根。

    要 step 里的文件走 `store.file_path` / `store.open_file` / `store.scratch_path`，
    要视频段走 `hls.*`；
    要存储根**旁边**的东西（lab 导出根、lab 配置文件）在 `settings` 上加派生 property。
    """
    violations = []
    for rel, tree in _iter_app_trees():
        if any(rel.startswith(p) or rel == p for p in STORAGE_ROOT_ALLOWED):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == STORAGE_ROOT_ATTR:
                violations.append(f"{rel}:{node.lineno} → settings.{STORAGE_ROOT_ATTR}")
            # 包内私有的根解析函数，import 或调用都算拿到了根
            elif isinstance(node, ast.Attribute) and node.attr == STORAGE_ROOT_FN:
                violations.append(f"{rel}:{node.lineno} → store.{STORAGE_ROOT_FN}()")
            elif isinstance(node, ast.ImportFrom) and any(
                a.name == STORAGE_ROOT_FN for a in node.names
            ):
                violations.append(f"{rel}:{node.lineno} → import {STORAGE_ROOT_FN}")

    assert not violations, (
        "以下文件拿到了存储根：\n  "
        + "\n  ".join(violations)
        + "\n落盘位置一律问 step_store（Step.product_path / open_product / scratch_path）；"
        "\n存储根旁边的东西在 settings 上加派生 property，别向 step_store 借根。"
    )


def test_step_store_write_members_have_registered_callers():
    """`Step` 的写成员只许被登记在案的三个 writer 模块调用。

    防的是「计划外的第四个写者混进来」。写权限**不做类型级隔离**（句柄可随手构造，
    发一个 `StepWriter` 类型只是命名仪式、零强制力）—— Python 里真正拦得住的是运行时
    校验与静态门禁，故就拦在这一处。
    """
    violations = []
    for rel, tree in _iter_app_trees():
        if any(rel.startswith(p) or rel == p for p in WRITE_MEMBER_ALLOWED):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITE_MEMBERS
            ):
                violations.append(f"{rel}:{node.lineno} → .{node.func.attr}()")

    assert not violations, (
        "以下文件调用了 step_store 的写成员，但不在 writer 白名单里：\n  "
        + "\n  ".join(violations)
        + "\n新增写者先加进 WRITE_MEMBER_ALLOWED 并写明它往 step 目录里写什么。"
    )


def test_step_store_internals_are_package_private():
    """`_layout` / `_playlist` / `_decoder` 只许包内 import，对外只有 store 与 hls。

    包外自己 import 内部模块，等于又开始自己拼文件名、自己备 m3u8 的料，而备料
    （EXTINF 真值、滤在途、判 init、算 TARGETDURATION）才是写错会**静默**的那部分——
    骨架写错播放器立刻报错，备料写错表现为 hls.js 段尾停摆、缓冲洞、导出时长错乱。
    具名例外与各自的退出条件见 PRIVATE_MODULE_ALLOWED。
    """
    pkg = LEAF_PACKAGE.replace("/", ".")
    short = {m.rsplit(".", 1)[1]: m for m in PRIVATE_MODULES}
    violations = []
    for rel, tree in _iter_app_trees():
        if rel.startswith(f"{LEAF_PACKAGE}/"):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            hits = set()
            if node.module in PRIVATE_MODULES:
                hits.add(node.module)
            elif node.module == pkg:
                hits |= {short[a.name] for a in node.names if a.name in short}
            for mod in sorted(hits):
                if rel not in PRIVATE_MODULE_ALLOWED.get(mod, set()):
                    violations.append(f"{rel}:{node.lineno} → {mod}")

    assert not violations, (
        "以下文件 import 了 step_store 的包内私有模块：\n  "
        + "\n  ".join(violations)
        + "\n落盘位置问 `store`，视频的一切问 `hls`——拼 VOD m3u8 走"
        "\n`hls.vod_playlist(task_id, step_id, track, ...)`，它把 EXTINF 真值、"
        "\n在途段过滤、init 判据与 TARGETDURATION 一并备好。"
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
