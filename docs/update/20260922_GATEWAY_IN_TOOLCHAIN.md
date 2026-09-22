# 网关进程纳入 Python 工具链：覆盖率统计两个进程、pytest 自带 pythonpath、去掉入口的 sys.path 与模块级 basicConfig

> **变更状态**：生效中（2026-09-22）——全量 `pytest` 827 passed，网关 `python -m` 实跑验过（起 MediaMTX、日志格式不变、退出无孤儿进程）
> **知识库**：待沉淀

## 概述

仓库跑两个进程（`app/` 后端 + `mediamtx_gateway/` RTSP 网关），但 Python 侧的工具配置只认 `app/`。四处对齐：

1. `[tool.coverage.run] source` 加上 `mediamtx_gateway`，覆盖率报告不再漏掉 348 行网关代码；
2. `[tool.pytest.ini_options]` 加 `pythonpath = ["."]`，裸 `pytest` 从此能跑（此前只有 `python -m pytest` 行）；
3. `mediamtx_gateway/main.py` 去掉 import 期的 `sys.path.insert` 和模块级 `logging.basicConfig`；
4. `tests/test_mediamtx_gateway.py` 的 importlib 按路径加载改成普通 import。

## 变更背景

- **覆盖率漏进程**：`source = ["app"]` 是单进程时代的残留。网关有完整单测（`TestLoadConfig` / `TestRTSPProxy` / `TestRunMediamtx`）却从不出现在报告里，实测补上后是 `main.py` 58.0% / `rtsp_proxy.py` 84.0%。
- **裸 `pytest` 一直是坏的**：仓库不是可安装包，没有根 `conftest.py`、没有 `pythonpath` 配置，`app` 能被 import 纯靠 `python -m pytest` 隐式把 cwd 塞进 `sys.path`。按 CLAUDE.md / README 写的 `pytest tests/` 直接 `ModuleNotFoundError: No module named 'app'`。
- **测试里的 importlib hack 是这个缺陷的伴生物**：`tests/test_mediamtx_gateway.py` 按文件路径 exec `main.py`，注释写的理由是「`scripts/mediamtx_gateway/main.py` 无 `__init__.py`」——目录早搬出 `scripts/`、`__init__.py` 也有了。更麻烦的是它顺带执行了 `main.py` 的 `sys.path.insert(repo_root)`，于是该文件后面那行 `from app.utils.gateway import ...` 是靠这个副作用才成立的，导入顺序成了隐性依赖。

## 方案详情

### 1. `pyproject.toml`

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]                       # 新增：仓库根进 sys.path，裸 pytest 可用

[tool.coverage.run]
source = ["app", "mediamtx_gateway"]     # 原 ["app"]
branch = true
omit = ["app/main.py", "*/__pycache__/*"]
```

用法从 `--cov=app` 改成不带值的 `--cov`——**命令行 `--cov=app` 会覆盖配置里的 source**，写成带值的等于没改。README 测试章节同步。

`mediamtx_gateway/main.py` 没进 `omit`：和 `app/main.py` 那种纯 lifespan 壳不同，它的 `_load_config` / `_run_mediamtx` 是被单测直接打的逻辑。

### 2. 网关入口不再有 import 期副作用

```python
# 删
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # 模块级
logging.basicConfig(level=..., format=..., datefmt=...)           # 模块级

# 后者移进 _main()，与 stage_worker.py 的做法一致（日志配置属进程入口，不属模块）
```

`sys.path` 那行是为支持 `python mediamtx_gateway/main.py`（裸脚本路径）而存在的。该形式全仓库无人使用——`start_backend.sh` / `.ps1` 和 CLAUDE.md 都走 `python -m mediamtx_gateway.main`，只有 main.py 自己的 docstring 在宣传它。**该启动方式就此取消**，docstring 改成只留 `-m` 一种并注明须在仓库根执行；继续用裸脚本路径起的会撞 `ModuleNotFoundError`，改 `-m` 即可。

### 3. 测试改普通 import

```python
from mediamtx_gateway import main as gw_main        # monkeypatch 模块级 _CONFIG_PATH 用
from mediamtx_gateway.main import _MAX_RESTARTS, _load_config, _run_mediamtx
```

原先的 `monkeypatch.setattr(_gw_mod, ...)` 相应换成 `gw_main`。

## 影响面

| 面 | 变化 |
|----|------|
| 运行时行为 | 网关日志输出格式、时机均不变（`basicConfig` 仍在任何日志调用前执行）；唯一减少的是裸脚本路径启动 |
| 测试 | 全量 827 passed；裸 `pytest` 与 `python -m pytest` 现在都行 |
| 覆盖率数字 | 分母变大（+348 行），历史基线不可直接比 |
| 部署脚本 | 无改动，本来就是 `-m` |

## 遗留

- `docs/kb/TESTING_MAP.md` 第 108 行仍写着 `source=app`、`pytest tests/ --cov=app`，已过期，留给知识库融合时一并改。
- 该文件第 110 行的覆盖率基线（2026-07-05，54.7%→57.3%）是 `--cov=app` 口径，与新口径不可比。
