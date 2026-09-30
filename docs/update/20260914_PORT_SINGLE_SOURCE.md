# 端口收敛为「启动脚本唯一声明」，消除 `.env` 与脚本的双真源

> **变更状态**：生效中（2026-09-15）　<!-- 三级优先级方案曾短暂落地，同批次内已撤回，最终形态为脚本内声明 -->
> **知识库**：已沉淀 → [SERVICE_CONFIG.md](../kb/SERVICE_CONFIG.md)（2026-09-20）

## 概述

全栈五个端口的运行时真源收敛到启动脚本里的五行 `BASE_*` / `$Base*`：改端口只改那五行，脚本把结果以环境变量注入后端、网关与 MediaMTX，压过 `.env*`、`mediamtx.yml`、`config.ini`、`settings.py` 中的同名值。默认端口与 test +100 偏移规则不变。顺带修掉 `.env` 静默覆盖环境变量、后端端口双真源两个既有缺陷。

## 变更背景

- **现状 / 痛点**：端口硬编码在两个启动脚本里，改端口只能改脚本；同时 `settings.py`、`mediamtx.yml`、`config.ini` 各有一份默认值，「到底改哪」没有明确答案。
- **触发来源**：阿里 PPU 备用机 `8.130.213.80` 的 8000 被同 netns 的其他容器占着、拿不回来，平台只开放 20010-20020 段（其中 20010-20015 已被占），需要把后端与网关 RTSP 整体挪走。承接 [20260912_PPU_PLATFORM_DEPS.md](20260912_PPU_PLATFORM_DEPS.md) 的端口核查结论。
- **顺带暴露的两个既有缺陷**：

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | `_load_env_files()` 用 `os.environ[k] = v` 无条件覆盖，`.env` 赢过 shell export | 潜在正确性：脚本注入的值被文件静默盖掉，无报错 |
| #2 | 后端端口两个真源：脚本走 `uvicorn --port`，`python -m app.main` 走 `settings.port` | 两条启动路径绑不同端口 |

## 方案详情

### 全景：五行声明 → 三组环境变量 → 三个进程

```text
start_backend.sh / .ps1 的 BASE_*（唯一声明）
        │  + test 偏移 100
        ├─ CLEANSIGHT_PORT / _MEDIAMTX_PROXY_PORT / _INTERNAL_PORT ─→ 后端（uvicorn + settings）
        ├─ GATEWAY_LISTEN_PORT / GATEWAY_TARGET_PORT ──────────────→ 网关（不读 .env*）
        └─ MTX_RTSPADDRESS / MTX_RTPADDRESS / MTX_RTCPADDRESS ─────→ MediaMTX（不读 .env*）
```

三组消费方键名各不相同且后两者不读 `.env*`，所以**必须由脚本转译注入**——这也是「端口不适合放 `.env`」的根因。

| 步骤 / 部件 | 落在哪 | 详见 |
|------------|--------|------|
| 端口声明与派生 | `start_backend.sh` / `start_backend.ps1` | §1 |
| 注入值压过 `.env*` 的前提 | `app/settings.py` | §2 |
| 会话变量不外泄 | `start_backend.ps1` | §3 |
| 集成测试跟随端口 | `integration_tests/test_traceback.py` | §4 |

### 方案选型

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| **脚本内声明（采用）** | 两个平台两份声明需同步；部署机改脚本留 git 本地 diff | 单一可写位置，无优先级歧义 |
| 真源放 `.env*`，三级优先级（shell env > `.env*` > 脚本基准） | 端口有两个可写位置，且 `.env*` 静默赢过脚本 | **曾落地，已撤回**——与「只需改一处」直接冲突 |
| 新开 `ports.conf` 给两脚本共读 | 单一真源且不偏向平台，但配置入口从 1 个变 2 个 | 否 |
| ps1 从 `start_backend.sh` 解析那五行 | 字面意义的单一真源，但 Windows 机要去改 bash 文件 | 否 |

### 1. `start_backend.sh` / `start_backend.ps1` — 端口声明与注入

```bash
# ===== 端口（唯一声明处：改端口只改这五行）=====
BASE_BACKEND=8000     BASE_PROXY=8004     BASE_INTERNAL=18004
BASE_RTP=8002         BASE_RTCP=8003
# test 整体 +100；其余全部派生并 export
```

新增 `export CLEANSIGHT_PORT=$BACKEND_PORT`（修 #2）。两个脚本头部写明「唯一声明处」及「两边需同步」。

### 2. `app/settings.py` — `.env` 不再覆盖已存在的环境变量

`_load_env_files()` 里 `os.environ[k] = v` → `os.environ.setdefault(k, v)`（修 #1）。

这是「脚本为唯一真源」成立的前提：脚本导出的端口必须压得过 `.env*` 里可能存在的同名残留。原优先级 `.env` > shell export 反直觉，也与网关自身文档写的「环境变量 > config.ini > 默认值」相反。

> **影响面不止端口**：所有 `CLEANSIGHT_*` 现在都是 shell 优先。实测 `CLEANSIGHT_DB_HOST=9.9.9.9` 能压过 `.env.dev` 的 `127.0.0.1`。这是期望行为，但属跨全局的语义变更。

### 3. `start_backend.ps1` — 导出前快照，退出时还原

`.ps1` 跑在调用方进程内（不像 bash 的 `./script.sh` 跑子进程），`$env:X = ...` 在脚本退出后仍留在那个窗口。新增的 `CLEANSIGHT_PORT` 导出因此有一条泄漏路径：跑完 `test` 再在同一窗口手动 `python -m app.main`，后端会读到残留的 `CLEANSIGHT_PORT=8100` 而非 `.env` 的值，且毫无提示。

处置：导出前快照八个变量（三 `CLEANSIGHT_*` + 二 `GATEWAY_*` + 三 `MTX_*`），`finally` 还原。

> **还原到快照而非直接删除**：用户自己 `$env:CLEANSIGHT_PORT=X` 后再调脚本，那是他的会话变量，应保留。端口占用自检同时挪到导出之前，使其 `exit 1` 早退路径也不留污染。副作用：原本永久留在会话里的 `GATEWAY_*` / `MTX_*`（既有行为）现在一并清掉。

### 4. `integration_tests/test_traceback.py` — 补 `--api-port`

原来只有 `--server`，8000 硬编码在 4 处。flag 名与 `test_single_client.py` / `test_multi_client.py` 对齐。

### 5. 保留项（刻意不改）

- `app/settings.py` 的 `port` / `mediamtx_proxy_port` / `mediamtx_internal_port` 字段默认值
- `mediamtx/mediamtx.yml` 的 `rtspAddress` / `rtpAddress` / `rtcpAddress`
- `mediamtx_gateway/config.ini` 的 `listen_port` / `target_port`

这三处是「脱离启动脚本单独跑某个进程」时的回退值，经脚本启动时全部被环境变量压过，不构成第二真源。

### 6. 文档

`.env.example` 的「服务端口」段改为指路（端口不在此配、去哪改、在此写 `CLEANSIGHT_PORT` 不生效）；`DEPLOYMENT.md` 端口段同步。**未动 `docs/kb/`**。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 改端口要动几处 | 脚本 + 按情况还要查另外三处默认值 | 脚本里五行（每平台一份） |
| `.env` 与 shell export 的优先级 | `.env` 赢，且无提示 | shell export 赢（符合通行约定） |
| 后端端口真源 | 2 个（脚本 / `settings.port`），可分叉 | 1 个 |
| ps1 对调用方会话的污染 | 永久留下 `GATEWAY_*` / `MTX_*` | 退出即还原 |

**自测结果**

sh 抽出端口块、ps1 抽出从声明到 `finally` 的完整流程（起网关与 uvicorn 两行换成打印），均不启动真实服务。

| 项 | 结果 |
|----|------|
| dev → 8000/8004/18004/8002/8003 | ✅ 两侧 |
| test → 8100/8104/18104/8102/8103 | ✅ 两侧 |
| `.env.dev` 写 `CLEANSIGHT_PORT=29999`，不生效 | ✅ 两侧 |
| shell 环境变量 `CLEANSIGHT_PORT=29997`，不生效 | ✅ sh |
| 同一会话先 dev 再 test，不被上轮残留干扰 | ✅ ps1 |
| 脚本退出后会话无残留 | ✅ ps1 |
| 注入链路 `GATEWAY_*` / `MTX_*` / `CLEANSIGHT_PORT` 取值正确 | ✅ 两侧 |
| `start_backend.ps1` 语法（`ParseFile`） | 0 错误 |
| `settings` 优先级实测 | shell env 压过 `.env.dev`；无 shell env 时回落文件值 |
| 全量 `pytest tests/` | **738 passed** |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 对外两口必须 1:1 NAT 映射 | 非等值映射下 `_rewrite_rtsp_url` 认不出本机 MediaMTX，后端绕公网回源，多数环境不通 | 已写进 `.env.example` 与 `DEPLOYMENT.md`；代码层面无防呆 |
| 两个脚本各一份声明 | 改了 Linux 忘了 Windows → 两端口漂移 | 已在两脚本头部标注；暂不做机制约束 |
| 未跑端到端 `test_single_client.py` | 改端口后回源改写是否仍正确没有实证 | 需真实 DB + 告警端点（写库发告警），部署时与人确认后补跑 |
| PPU 机实际端口未定 | 20016/20017 是按 09-12 占用快照选的 | 部署当天重扫 20016-20020 |
| `start_backend.ps1` 换行符是隐性依赖（**既有问题，非本次引入**） | 该文件无 BOM + 中文注释，PS 5.1 按 GBK 读：CRLF 时被吃掉的是 `\r`（无害），**LF 时被吃掉的是 `\n`，下一行并入注释直接解析失败**。git 存 LF，靠 `core.autocrlf=true` 才正常，而仓库无 `.gitattributes` | 根治需加 `.gitattributes` 钉 `*.ps1 text eol=crlf` 或补 BOM；本次仅规避——两脚本的用户可见输出统一保持英文 |
