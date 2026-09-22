---
name: deploy
description: "CleanSightBackend 的部署与环境搭建总入口：先判定目标平台（Linux NVIDIA / Windows NVIDIA / 阿里 PPU / 构建机打物料）与角色（prod / dev / test），再按平台读取对应的安装、配置、启动、验证规范。凡是用户提到部署、安装依赖、装环境、搭开发机、跑 install.sh / install.ps1 / build.sh、传仓库到服务器、配免密、物料源机、wheelhouse、ffmpeg / MediaMTX 二进制、.env 配置、端口冲突、启动后端起不来、start_backend 报错、e2e 验证、升级 torch / ffmpeg / MediaMTX 版本，都用本 skill，即便用户没说「部署」二字。deploy、install、setup environment、offline install、deploy to server、run install.sh、build materials、port conflict 也算。"
---

# CleanSight 部署

仓库里**没有独立的部署文档**，本 skill 就是部署规范的唯一入口。事实源是三个脚本开头的配置块（[build.sh](../../../build.sh)、[install.sh](../../../install.sh)、[install.ps1](../../../install.ps1)）与两个启动脚本的端口声明；本 skill 与脚本不符时以脚本为准，并回来改本 skill。

## 第一步：定平台，决定装什么

安装路径由**平台**决定，三条路径互斥（PPU 机跑 `install.sh` 会毁掉厂商 torch；Windows 用不了 Linux 的 wheelhouse），装错不是「慢一点」而是「装废」。从用户描述、`~/.ssh/config`、或直接在目标机跑判别命令确认，判不出来就问：

| 平台 | 判别 | 安装脚本 | 读这份 |
|------|------|----------|--------|
| **Linux + NVIDIA**（x86_64） | `uname -m` 为 x86_64，`nvidia-smi` 头部 `NVIDIA-SMI` | `./install.sh` | [references/linux.md](references/linux.md) |
| **Windows + NVIDIA** | 目标是本机或某台 Windows，PowerShell | `.\install.ps1` | [references/windows.md](references/windows.md) |
| **阿里 PPU** | `nvidia-smi` 头部 `PPU-SMI`，`/dev/alixpu_ppu*`，`/usr/local/PPU_SDK` 存在 | 无脚本，手动 | [references/ppu.md](references/ppu.md) |
| **构建机打物料** | 用户要重建 wheelhouse / vendor、升级版本、源机缺物料 | `./build.sh` | [references/materials.md](references/materials.md) |

没有 CPU-only 路径，已淘汰。

## 第二步：定角色，决定怎么起

同一平台装完后，**角色**只影响用哪份 `.env*`、启动参数和端口偏移，与安装路径无关。Linux 开发机就是「Linux 安装路径 + dev 角色」，与生产装法完全相同。

| 角色 | 配置文件 | 启动 | 端口 | 用途 |
|------|----------|------|------|------|
| prod | `.env` | `start_backend.sh prod` | 基准 | 真实库、真实告警，**开发不碰** |
| test | `.env.test` | `start_backend.sh test` | 基准 +2 | 独立测试库，可与同机 prod 并存 |
| dev | `.env.dev` | `start_backend.sh dev`（Windows 用 `.ps1`） | 基准 | 本地库或不可达地址，带 `--reload` |

`.env*` 全在 `.gitignore` 里，目标机要自己写，字段见 [references/runtime-config.md](references/runtime-config.md)。

## 平台一览

```text
              Linux + NVIDIA        Windows + NVIDIA        阿里 PPU
安装          ./install.sh          .\install.ps1           手动（ppu.md）
依赖清单      requirements/prod     requirements/gpu        requirements/ppu
torch 来源    源机 wheelhouse       cu128 索引在线          系统 site-packages（厂商版）
ffmpeg/MTX    源机 vendor           源机 vendor             源机 vendor（手动拉）
Python        精确 3.10             3.10–3.13               系统 3.12
启动          ./start_backend.sh    .\start_backend.ps1     ./start_backend.sh
打物料        ./build.sh 只在 Linux 构建机跑一次，产物含 Windows 包，经源机分发给三者
```

## 所有平台都遵守的硬约束

- **三类物料统一走源机** `http://49.234.120.241:8088`（`install.sh` / `install.ps1` 开头写死，`BASE_URL` 环境变量可临时覆盖）。没有本地物料旁路、没有 GitHub 在线旁路，**install 前源机分发服务必须在线**，否则在下载第一个物料时就失败。源机怎么开见 [materials.md](references/materials.md)。
- **ffmpeg / MediaMTX 装在项目内**（`.ffmpeg/`、`mediamtx/`），后端不回退系统 PATH。不要 `apt install ffmpeg` 之类去凑，ffmpeg 4.x / 8.x 对 HLS 参数的解析差异会把录制写坏。
- **端口只在启动脚本里声明**：`start_backend.sh` 的 `BASE_*` 五行、`start_backend.ps1` 的 `$Base*` 五行，两份独立、改要同步。启动脚本一条命令拉起网关（含 MediaMTX）+ 后端，**不要再手动单独起 MediaMTX**，会撞 18004。
- **`.env*` 必填六项**（DB 五项 + `CLEANSIGHT_ALARM_REPORT_URL`），缺一项 `app.settings` 导入即抛 `ValidationError`，任何角色都起不来。
- **开发只跑 dev / test，不碰 prod**；端到端测试会建测试任务、推流、可能发告警，目标库或告警端点是共享资源时先问用户。

## 通用流程骨架

每份平台参考都按这个顺序展开，遇到哪步去对应文件找细节：

```text
[0] 定平台 + 定角色（上两表）
[1] 目标机预检：OS / Python / GPU / curl / 磁盘 / 源机连通
[2] 源机分发服务在线
[3] 跑安装脚本（Linux 装 20–40 分钟，nohup + 轮询日志）
[4] 写 .env*（runtime-config.md）
[5] 确认五个端口空闲，启动
[6] 验证：/health/status（不是裸 /health）→ 8004 与 18004 都在听 → e2e
```

## 汇报

按 CLAUDE.md 的规则：只上报阻塞、正确性错误、数据/安全风险；给决策就列选项与代价。部署完成时把平台参考末尾的「部署完成检查清单」逐项对一遍再说完成。
