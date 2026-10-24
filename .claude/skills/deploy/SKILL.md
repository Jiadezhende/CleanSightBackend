---
name: deploy
description: "CleanSightBackend 的部署与环境搭建总入口：按平台（Linux NVIDIA / Windows NVIDIA / 阿里 PPU / 构建机打物料）装依赖，按角色（prod / dev / test）初始化 .env / .env.dev / .env.test 并启动验证，跑真推理前核对模型权重。凡是用户提到部署、安装依赖、装环境、搭开发机、初始化或补 .env 配置、开关限流 / 白名单、跑 install.sh / install.ps1 / build.sh、传仓库到服务器、配免密、物料源机、wheelhouse、ffmpeg / MediaMTX 二进制、模型权重缺失、端口冲突、启动后端起不来、start_backend 报错、e2e 验证、升级 torch / ffmpeg / MediaMTX 版本，都用本 skill，即便用户没说「部署」二字。deploy、install、setup environment、init env、offline install、deploy to server、run install.sh、build materials、port conflict 也算。"
---

# CleanSight 部署

仓库里**没有独立的部署文档**，本 skill 就是部署规范的唯一入口。事实源是三个脚本开头的配置块（[build.sh](../../../build.sh)、[install.sh](../../../install.sh)、[install.ps1](../../../install.ps1)）与两个启动脚本的端口声明；本 skill 与脚本不符时以脚本为准，并回来改本 skill。

## 输入

| 项 | 来源 | 缺省 |
|----|------|------|
| 物料源机 `BASE_URL` | 用户给，普通参数 | 安装脚本开头写死的默认值 |
| 角色 prod / dev / test | 用户给，一台机可多个（如 prod + test） | 无缺省，问 |
| 目标机 | 用户要求 | **本机**；用户点名远程机才走 SSH，要一个 SSH 别名 |

DB 连接、告警端点、Label Studio token、白名单 IP **不是输入**：由用户自己写进 `.env*`，AI 全程不读值（见硬约束第一条）。模型权重也不是输入：用户从魔搭手动下载，AI 只核对在不在。

## 主流程

```text
━━ 装环境（按平台，与角色无关；一台机装一次）━━
[1] 定目标机：缺省本机；远程见 linux.md [1][2]
[2] 判平台（下表）→ 读对应 reference
[3] 预检：OS / Python / GPU / curl / 磁盘 / BASE_URL 连通
[4] 源机分发服务在线 → BASE_URL=<用户给的> 跑安装脚本

━━ 配 .env*（按角色，可重复执行）━━
[5] 按 runtime-config.md「按场景启用变量」写非敏感项；文件不存在时以 .env.example 为底
[6] 交接：告诉用户打开哪个文件、填哪几个键 → 等用户说填好
[7] 键检查：只输出「已填 / 缺失 / 占位符」，不输出值

━━ 起服务（按角色）━━
[8] 端口空闲 → start_backend <role> → /health/status、对外 RTSP 口与 MediaMTX 口都在听

━━ 跑真推理前 ━━
[9] 权重核对：列出缺失文件，提示用户从魔搭下载放到哪 → 等用户补齐再核一次
[10] e2e（有副作用，先问）
```

权重缺失不挡 [8]：推理子进程 warmup 失败只记日志、不阻止启动，所以健康检查能先过；但不补齐就推不出结果，[10] 和交付真实任务之前必须过 [9]。

## 定平台：决定装什么

安装路径由**平台**决定，三条路径互斥（PPU 机跑 `install.sh` 会毁掉厂商 torch；Windows 用不了 Linux 的 wheelhouse），装错不是「慢一点」而是「装废」。从用户描述、`~/.ssh/config`、或直接在目标机跑判别命令确认，判不出来就问：

| 平台 | 判别 | 安装脚本 | 读这份 |
|------|------|----------|--------|
| **Linux + NVIDIA**（x86_64） | `uname -m` 为 x86_64，`nvidia-smi` 头部 `NVIDIA-SMI` | `./install.sh` | [references/linux.md](references/linux.md) |
| **Windows + NVIDIA** | 目标是本机或某台 Windows，PowerShell | `.\install.ps1` | [references/windows.md](references/windows.md) |
| **阿里 PPU** | `nvidia-smi` 头部 `PPU-SMI`，`/dev/alixpu_ppu*`，`/usr/local/PPU_SDK` 存在 | 无脚本，手动 | [references/ppu.md](references/ppu.md) |
| **构建机打物料** | 用户要重建 wheelhouse / vendor、升级版本、源机缺物料 | `./build.sh` | [references/materials.md](references/materials.md) |

没有 CPU-only 路径，已淘汰。

## 定角色：决定怎么配、怎么起

同一平台装完后，**角色**只影响用哪份 `.env*`、启动参数和端口偏移，与安装路径无关。

| 角色 | 配置文件 | 启动 | 端口 | 用途 |
|------|----------|------|------|------|
| prod | `.env` | `start_backend.sh prod` | 基准 | 真实库、真实告警，**开发不碰** |
| test | `.env.test` | `start_backend.sh test` | 基准 +2 | 独立测试库，可与同机 prod 并存 |
| dev | `.env.dev` | `start_backend.sh dev`（Windows 用 `.ps1`） | 基准 | 开发，带 `--reload` |

各角色、各场景该启用哪些变量，以及不读值的写法，见 [references/runtime-config.md](references/runtime-config.md)。

## 所有平台都遵守的硬约束

- **`.env*` 只按键操作，不读值**：不用 Read 工具打开、不 `cat` / `head` / `less`、不 `source` 后 `env` / `printenv`、不整段 `tail` 后端日志（[app/main.py:55](../../../app/main.py#L55) 会打 DB 地址，连库失败的报错会带 host 与用户名）。允许的操作只有 runtime-config.md 给的那几种：按键名判在不在、覆写非敏感键、追加键、检查状态。需要看完整报错时给用户一条命令让他自己跑、脱敏后贴回来。
- **ffmpeg / MediaMTX / torch 闭包统一走源机 `BASE_URL`**，没有本地物料旁路、没有 GitHub 在线旁路，**install 前源机分发服务必须在线**。源机怎么开见 [materials.md](references/materials.md)。**模型权重不走源机**，用户从魔搭手动下载。
- **ffmpeg / MediaMTX 装在项目内**（`.ffmpeg/`、`mediamtx/`），后端不回退系统 PATH。不要 `apt install ffmpeg` 之类去凑，ffmpeg 4.x / 8.x 对 HLS 参数的解析差异会把录制写坏。
- **端口只在启动脚本里声明**：`start_backend.sh` 的 `BASE_*` 五行、`start_backend.ps1` 的 `$Base*` 五行，两份独立、改要同步。启动脚本一条命令拉起网关（含 MediaMTX）+ 后端，**不要再手动单独起 MediaMTX**，会撞 18004。
- **`.env*` 必填六项**（DB 五项 + `CLEANSIGHT_ALARM_REPORT_URL`）。prod / test 缺一项启动即抛错；dev 只告警，但连不上库后端照样不可用。
- **开发只跑 dev / test，不碰 prod**；prod 的 `.env` 只在用户明确要部署生产时初始化。端到端测试会建测试任务、推流、可能发告警，目标库或告警端点是共享资源时先问用户。

## 汇报

按 CLAUDE.md 的规则：只上报阻塞、正确性错误、数据/安全风险；给决策就列选项与代价。部署完成时把平台参考末尾的「部署完成检查清单」逐项对一遍再说完成；权重未补齐要单列出来，不能算完成。
