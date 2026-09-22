# 仓库根目录收敛：配置/依赖/素材各归其位，顺带修掉三个既有缺陷

> **变更状态**：生效中（2026-09-22）
> **知识库**：待沉淀

## 概述

根目录从 27 项降到 21 项：建 `pyproject.toml` 收 pytest/coverage 配置，三份 requirements 拆成 `requirements/` 下的 base + 三变体，日志配置进 `config/`，测试视频进 `integration_tests/fixtures/`，删 `docs/archive/` 与三个空壳目录。零行为变更，`pytest` 827 passed 与改前一致。

## 变更背景

- **现状 / 痛点**：根目录 20+ 条目里源码、运行时产物、第三方物料、配置平铺在一起。`test/`（4 个 mp4 素材）与 `tests/`（57 个单测）一字之差；pytest 完全没有配置文件而 `.coveragerc` 独自躺在根上；三份 requirements 互相手抄。
- **触发来源**：骨架合理性评审。`app/` 内部五层分层有 `tests/test_import_hygiene.py` 门禁守着、本次不动，问题全在外围。
- **核查中发现的三个既有缺陷**，一并修掉：

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | `lap>=0.5.12` 在 `requirements.txt` 与 `-ppu` 里有，`-cpu` 那份漏了 | CPU 机装完缺依赖，三份手抄的必然结果 |
| #2 | `settings.log_config` 是半个开关：两个 start 脚本硬编码路径直接绕过它 | 设了 `CLEANSIGHT_LOG_CONFIG` 走脚本启动不生效，静默 |
| #3 | 全仓库无一句说明模型权重怎么到目标机 | 新部署机可能缺模型，且无处可查 |

## 方案详情

### 全景：三类东西、三个去处

命名与落点的判据是**谁在什么时候读它**，不是它是什么格式：

```text
Python 工具配置（pytest / coverage）  →  pyproject.toml（只有 [tool.*]）
依赖清单（按部署路径）                →  requirements/{base,prod,gpu,ppu}.txt
运维要改的配置（服务 yaml + 日志）    →  config/
测试素材（只有集成测试读）            →  integration_tests/fixtures/
无人引用的空壳 / 旧档                 →  删
```

| 步骤 / 部件 | 落在哪 | 详见 |
|------------|--------|------|
| pytest + coverage 配置收编 | `pyproject.toml`、删 `.coveragerc` | §1 |
| 依赖拆层 + 补 `lap` | `requirements/`、`install.sh` / `install.ps1` | §2 |
| 日志配置搬家 + 删半个开关 | `config/logging.json`、`app/settings.py`、`app/main.py`、两个 start 脚本 | §3 |
| 测试素材跟消费者走 | `integration_tests/fixtures/` | §4 |
| 空壳 / 旧档删除 | `tests/unit`、`tests/component`、`app/algorithm`、`tmp/`、`docs/archive/` | §5 |
| 权重停跟踪 + 补分发说明 | `app/data/*.pt`、`docs/DEPLOYMENT.md` | §6 |
| 文档同步 | `README.md`、KB 两份 | §7 |

### 1. `pyproject.toml` — 给 Python 工具一个落点

**只写 `[tool.*]`，不写 `[project]` 也不写 `[build-system]`**：本仓库不是可安装包（部署靠 git archive 取码、进程用 `python -m app.main`），加上那两段只会让 pip 与构建后端把它当待打包项目。

- `[tool.coverage.*]`：`.coveragerc` 整体搬入，原文件删除。
- `[tool.pytest.ini_options]`：**只设 `testpaths = ["tests"]`**，裸 `pytest` 即只跑单元/组件测试。刻意不设 `asyncio_mode` / `addopts`——此前没有任何 pytest 配置文件，新增项都可能改变现有 827 个用例的行为，本批的约束是零行为变更。

### 2. `requirements/` — base 底座 + 三条部署路径各一份

```text
base.txt    平台无关全集（不含 torch，也不含 numpy），补回 lap>=0.5.12
prod.txt    -r base.txt + numpy==1.26.4                             ← Linux 生产，install.sh；torch 由 wheelhouse 装
gpu.txt     -r base.txt + numpy + --extra-index-url cu128 + torch==2.8.0+cu128   ← Windows GPU 开发机，install.ps1
ppu.txt     -r base.txt                                             ← torch/numpy 由系统 site-packages 提供
```

**按部署路径分，不按"能不能复用"分**：三条路径三份文件，install 脚本各装自己那份、不再单独拼 torch 安装步骤——原 `install.ps1` 里那段 `$TORCH_PKGS` + `--index-url cu128` 的两步安装，就是这么收进 `gpu.txt` 一行 `pip install -r` 的。ffmpeg / mediamtx 怎么拉不归 requirements 管，那是 install 侧（linux / win 两个脚本）的事。

**numpy 必须从 base 下放到各路径**：PPU 机器要的是「不装 numpy」（厂商 torch 按 numpy 2.x 编译，降到 1.26.4 立刻废，见 [20260912_PPU_PLATFORM_DEPS.md](20260912_PPU_PLATFORM_DEPS.md)），而 `-r` 只能加不能减。torch 同理不在 base：prod 从 wheelhouse 装、ppu 用系统的，只有 gpu 一份自己写。

**`gpu.txt` 的 torch 版本带 `+cu128` 后缀是刻意的**：安装时主索引是清华源，那里 `torch==2.8.0` 在 Windows 是 CPU 构建；钉住本地版本号让 pip 只可能从 extra index 命中，不会静默装错构建。

**原 `requirements-cpu.txt` 对应的 CPU-only 路径不再保留**：没有机器在用，删掉比留一份没人验证的清单干净。要在无 GPU 机上跑，用 `gpu.txt` 换 extra-index 为 `https://download.pytorch.org/whl/cpu`、版本后缀改 `+cpu` 即可。

不用 pyproject 的 `optional-dependencies` 收：`--extra-index-url` 与「刻意不装某包」这两件事 extras 表达不了，且 `install.sh` 是 `pip install -r` + 事后修 opencv / 钉回 numpy 的流程，换 extras 要重写安装脚本。

`pytest` 三件套暂不拆 `dev.txt`——备份机要跑测试（[20260915_BACKUP_HOST_DUAL_ENV.md](20260915_BACKUP_HOST_DUAL_ENV.md)），拆了那台得装两份。

调用点：`install.sh` 改 `-r requirements/prod.txt`；`install.ps1` 的 torch 单独安装步骤与 `requirements.txt` 安装合并为一行 `-r requirements/gpu.txt`；`docs/DEPLOYMENT.md` 流程描述同步。`docs/update/` 里的历史引用不回改（那些记录的是当时事实）。

### 3. 日志配置：搬进 `config/`，并把「半个开关」整个砍掉

`logging_config.json` → `config/logging.json`，与六份服务 yaml 同处（同一判据：运维要改的东西不该埋进 Python 包）。

**删 `settings.log_config`**，不是让脚本去尊重它。日志 dictConfig 文件不随环境变化，本就不该是开关；而现状是它存在、却被两个 start 脚本硬编码绕过（缺陷 #2）。现在三个调用点各写一次字面量：

| 调用点 | 旧 | 新 |
|--------|----|----|
| `app/main.py` `uvicorn.run` | `log_config=settings.log_config` | `log_config="config/logging.json"` |
| `start_backend.sh` | `--log-config logging_config.json` | `--log-config config/logging.json` |
| `start_backend.ps1` | 同上 | 同上 |

**删 `logging_config_fallback.json`**：全仓库零代码引用，只有 KB 一句「为兜底配置」却没说谁来兜、怎么切。`.env.example` 的 `CLEANSIGHT_LOG_CONFIG` 注释同步删除。

### 4. 测试素材跟着唯一消费者走

`test/*.mp4`（4 个，48M）→ `integration_tests/fixtures/`，同时消掉 `test/` 与 `tests/` 的近似撞名。路径解析由 `Path(__file__).parent.parent / "test"` 改为 `Path(__file__).parent / "fixtures"`（`test_single_client.py` / `test_multi_client.py` 各一处），文档引用同步（`integration_tests/README.md`、`docs/DEPLOYMENT.md`、`docs/QUICK_START.md`、`.claude/skills/deploy-linux/SKILL.md`）。

**入库的素材收敛到一份**：搬家前有两份 mp4 被跟踪（`test_video.mp4` 12M、`leak_test.mp4` 934K，都是在 `.gitignore` 规则之前进的库）。本次只留 `test_video.mp4`——`docs/DEPLOYMENT.md` 的部署后验证让运维直接跑 `test_single_client.py`，它默认取的就是这份，而部署机靠 `git archive` 取码，不跟踪等于每次部署多一趟手动拷贝。`leak_test.mp4` 停止跟踪（文件留在磁盘），与 `clean-test.mp4`（35M）、`leak.mp4` 一样走物料分发。`.gitignore` 规则为 `integration_tests/fixtures/*.mp4` + 一行 `!` 例外。

### 5. 删空壳与旧档

- `tests/unit/`、`tests/component/`——只剩 `__pycache__`，零文件、零引用，留着会让人以为存在分层约定。
- `app/algorithm/`（含 `colorstrip/`）——源码已删，只剩 pycache 残留。
- `tmp/`——空；代码里两处 `tempfile.TemporaryFile()` 走的是系统临时目录，不碰它。
- `docs/archive/`（50 份 KB 建立前的旧架构文档）——核查确认 README / CLAUDE.md / docs/kb / docs/api / DEVELOPMENT / DEPLOYMENT **零反向链接**，内容留在 git 历史里。删后 `docs/` 只剩 `kb/` `update/` `api/` 三个有路由的目录 + 顶层指南。

### 6. 权重停止跟踪 + 补上分发说明

`.gitignore` 早有 `app/data/*.pt`，但 `bend-best.pt`（22M）/ `bubble-best.pt`（6M）在规则之前就已跟踪。`git rm --cached` 二者，文件留在磁盘。**历史里那 28M 不清**（不做 filter-repo，代价是 clone 仍带着它们）。

顺带在 `docs/DEPLOYMENT.md` 新增「模型权重」小节（缺陷 #3）：权重不随 git 分发，从内部模型库按需取用，放到 `CLEANSIGHT_MODEL_PATH` 指向的目录。

### 7. 保留项（不改动）

- **`database/` 落盘根不改名**：它与 `app/database.py`（PostgreSQL 连接池）同名两指确实会误导，但改名要求每台机器停服搬目录，收益不抵成本，本次明确放弃。
- **`scripts/hospital_sync/` 位置不动**：只在 README 里点明它是独立交付物（医院数据同步的 sender 打包 + receiver systemd unit + SQL schema），不属后端主链路，消除「藏在 scripts/ 下像是运维脚本」的误读。
- **`app/data/` 目录位置不动**：权重仍在 Python 包内，只是不再随 git 走。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 根目录可见条目 | 27 | 21 |
| pytest 配置 | 无任何配置文件 | `pyproject.toml` 的 `[tool.pytest.ini_options]` |
| 依赖清单 | 3 份手抄、已漂（`lap` 漏在 cpu） | 1 份底座 + 3 条部署路径各一份只写差异（CPU-only 路径删除） |
| 日志配置路径 | 根目录，且有个被绕过的 env 开关 | `config/logging.json`，三处字面量、无开关 |
| 测试素材 | `test/`（与 `tests/` 撞名） | `integration_tests/fixtures/` |
| 权重入库 | 2 份 `.pt`（28M）被跟踪 | 已停跟踪，分发方式写进部署指南 |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest`（裸跑，验 `testpaths`） | **827 passed**，与改动前基线一致 |
| `pytest tests/test_mediamtx_gateway.py` | 12 passed（网关用例仍被 `testpaths` 收） |
| 覆盖率配置从 pyproject 生效 | `pytest --cov=app --cov-report=term-missing` 正常出报告，branch 列在、精度 1 位 |
| `pip install --dry-run -r requirements/{prod,gpu,ppu}.txt` | 三份均解析通过（清华源），`pip show lap` 有结果 |
| `logging.config.dictConfig(config/logging.json)` | 通过，5 个 handler 全部构造成功 |
| `from app.settings import settings` | 正常；`hasattr(settings, "log_config")` 为 False |
| 集成测试默认视频路径解析 | `integration_tests/fixtures/test_video.mp4` 存在 |
| 残留引用扫描 | `git grep` `logging_config` / `.coveragerc` / `requirements-cpu` / `requirements-ppu` / `docs/archive` / `log_config` 在 `docs/update/` 之外零命中 |

> 未跑真流端到端（`test_single_client.py`）：本批不碰运行链路，且它要写库、需先确认环境。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `install.sh` / `install.ps1` 的新依赖路径只做了 dry-run 验证 | 真机首次安装若路径写错会在装依赖这步失败（显式报错，非静默） | 下次上机部署时验证一次完整 `install.sh` |
| 覆盖率 `source` 仍只有 `app`，不含 `mediamtx_gateway/` | 网关是并列的第二个进程入口，12 个用例的覆盖从不计入数字 | 待定：加进 `source` 会让 TESTING_MAP 里那份 2026-07-05 的基线快照失效，需连带重测 |
| 历史里仍有 28M 权重 | clone 体积 | 不处理；要清须 filter-repo 改写历史，代价是所有协作者重新 clone |
| `app/utils/README.md` 与 `BOUNDARY_LAYER_EXAMPLES.md` 仍在代码目录 | 两份共 997 行描述性文档，且描述的能力一半零调用点、符号名已写错 | 下一批（纯文档）并入 `docs/kb/DESIGN_FAULT_TOLERANCE.md` |
| 旧 HLS 写侧代码仍在（`persistence/strategies/hls_strategy.py` 等） | 一行 `start()` 就能触发双 sweeper 抢帧的静默数据损坏，现防线只是一段注释 | 单独一批删除，碰运行代码、验收要带集成测试 |
