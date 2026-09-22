# 安装链路收敛：二进制统一走源机、删 deploy.conf、去掉 vendor SHA 钉版

> **变更状态**：生效中（2026-09-22）——脚本已改、语法与依赖解析已验，**尚未在真机跑过完整 install**
> **知识库**：待沉淀

## 概述

`install.sh` / `install.ps1` 的 ffmpeg 与 MediaMTX 只从源机 `${BASE_URL}/vendor/` 拉，不再有「本地物料」「上游在线下载」两条旁路；源机地址写死在两个脚本开头，常规部署直接 `./install.sh`。`deploy.conf` 删除，上游地址与 torch 版本进 `build.sh` 开头。vendor 的 SHA 钉版比对整套去掉。

## 变更背景

- **现状 / 痛点**：每次部署都要手敲 `BASE_URL=http://49.234.120.241:8088 ./install.sh`；`deploy.conf` 只剩十几行却是独立文件，被三个脚本各自解析（`install.ps1` 还要用正则去读 bash 变量）；ffmpeg / MediaMTX 有三条来源（本地 `vendor/`、源机、GitHub 上游），Windows 默认走的还是 GitHub 那条，出境限速下极慢；vendor 的 SHA 钉版每次升级都要「清空 → 跑 build → 回填 → 再跑一次」，收益不抵操作成本。
- **触发来源**：仓库结构整理（[20260922_REPO_LAYOUT_CLEANUP.md](20260922_REPO_LAYOUT_CLEANUP.md)）评审时，人定下三条：ffmpeg / MediaMTX 统一用源机分发最保险；SHA 校验去掉；`deploy.conf` 太薄不值得单独一个文件。
- **顺带否掉的方案**：对象存储放 vendor 物料——固定 URL、国内速度好，但要开云账号、有小额年费，最终决定继续用已经在跑的源机。

## 方案详情

### 全景：三类物料、三条来源，每条只有一个出口

```text
构建机 ./build.sh（开头配置块：上游 URL + TORCH_PKGS）
   ├─ wheelhouse/   torch 闭包 + SHA256SUMS（自动生成）
   └─ vendor/       ffmpeg / mediamtx 的 linux + win 四个包
         │ rsync
         ▼
源机 http://49.234.120.241:8088（systemd cleansight-dist，部署时起、部署完关）
         │
         ├─ install.sh  (Linux 生产)      torch ← wheelhouse（--require-hashes 逐 wheel 校）
         │                                ffmpeg / mediamtx ← vendor（xz -t / gzip -t 抓损坏）
         │                                其余 ← requirements/prod.txt @ 清华源
         └─ install.ps1 (Windows 开发机)  ffmpeg / mediamtx ← vendor
                                          torch + 其余 ← requirements/gpu.txt（cu128 经文件内 extra-index）
```

硬约束：**install 前源机必须在线**——去掉旁路后没有任何 fallback，源机不通就是装不了，报错在下载第一个物料时。

| 步骤 / 部件 | 落在哪 | 详见 |
|------------|--------|------|
| 上游地址 + torch 版本进 build.sh 开头 | `build.sh` | §1 |
| 源机地址写死、去本地物料分支 | `install.sh` | §2 |
| Windows 去 GitHub 在线分支、去 deploy.conf 解析 | `install.ps1` | §3 |
| vendor SHA 钉版整套删除 | `build.sh`、`install.sh` | §4 |
| 文档与 skill 同步 | `docs/DEPLOYMENT.md`、`.claude/skills/deploy-linux/SKILL.md` | §5 |

### 方案选型：配置放哪

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| 每个脚本开头一个配置块（采用） | torch 版本会出现在 `build.sh` 与 `requirements/gpu.txt` 两处 | `deploy.conf` 只剩上游 URL 与一行版本，不值得三个脚本各写一段解析；谁用的配置写在谁开头，`install.ps1` 不用再正则读 bash |
| 保留 `deploy.conf` 只留 URL | 文件更薄，解析代码原样保留 | 否：解析代码是它主要成本 |
| torch 版本抽成 `requirements/torch.txt` 供各处 `-r` | 单一真源 | 否：为复用多开一个文件，人明确不要 |

### 1. `build.sh` — 上游地址与 torch 版本进开头配置块

`source deploy.conf` 删除，改为文件开头一段 `═══` 框起来的配置块：`FFMPEG_URL` / `FFMPEG_WIN_URL` / `MEDIAMTX_URL` / `MEDIAMTX_WIN_URL` / `TORCH_PKGS` / `TORCH_INDEX_URL`。目标机永远不碰这些地址，它们只在构建机跑一次。

### 2. `install.sh` — 源机地址写死，三类物料一律走它

```bash
# 旧：deploy.conf 里 BASE_URL="${BASE_URL:-}"，空 = 用本地 wheelhouse/ + vendor/
# 新：脚本开头
BASE_URL="${BASE_URL:-http://49.234.120.241:8088}"
```

`${VAR:-default}` 保留环境变量覆盖，换源机照旧 `BASE_URL=... ./install.sh`。删掉的分支：

- torch 段的 `if [ -z "$BASE_URL" ]` 本地 wheelhouse 分支——只剩 HTTP 流式 + `--require-hashes` 那条
- ffmpeg / mediamtx 段的 `[ -n "$BASE_URL" ] && dl ...` 条件——改成无条件 `dl`
- 预检里「BASE_URL 为空则要求本地 wheelhouse/SHA256SUMS 存在」——改成「BASE_URL 必须非空」

`install.sh` 不再需要知道 torch 版本：wheelhouse 里就那一版，目录本身即钉版。

### 3. `install.ps1` — 与 Linux 同款

- 删 `Read-DeployConf` 函数与三个 `$conf[...]` 读取，改为开头配置块：`$BASE_URL`（同样写死默认、`$env:BASE_URL` 可覆盖）、`$PYPI_INDEX_URL`
- ffmpeg / mediamtx 的 `if ($BASE_URL) {源机} else {$FFMPEG_WIN_URL}` 三元改为只有源机那支
- torch 的两步安装（`pip install $TORCH_PKGS --index-url cu128` 再 `pip install -r requirements.txt`）合并为一行 `pip install -r requirements/gpu.txt -i 清华源`——cu128 索引与 `+cu128` 版本钉在 `gpu.txt` 里（见 [20260922_REPO_LAYOUT_CLEANUP.md §2](20260922_REPO_LAYOUT_CLEANUP.md)）

### 4. vendor SHA 钉版整套删除

删掉的：`build.sh` 的 `check_sha` 函数与两处调用、`install.sh` 的 `verify_sha` 函数与两处调用、原 `deploy.conf` 的 `FFMPEG_SHA256` / `MEDIAMTX_SHA256`。

**保留的**：`wheelhouse/SHA256SUMS` 的生成与 `install.sh` 的 `--require-hashes`。理由不同——vendor 那四个包只在构建机下载一次、经源机分发，下坏了 `xz -t` / `gzip -t` 当场抓出来；而 wheelhouse 是 6GB wheel 走 HTTP 流式安装、不落盘，`--require-hashes` 是唯一的完整性保障，且 SHA256SUMS 由 build.sh 自动写、不用人维护。

### 5. 保留项（不改动）

- `build.sh` 的构建机约束（Linux x86_64 + Python 3.10）与 wheelhouse 闭包完整性 dry-run 校验
- `install.sh` 末尾自检（torch / CUDA / cv2 / ultralytics / ffmpeg / mediamtx）
- 源机 `cleansight-dist` 的「部署时起、部署完关」流程——vendor 也从它拉，这条不变

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 常规部署命令 | `BASE_URL=http://... ./install.sh` | `./install.sh` |
| ffmpeg / mediamtx 来源 | 3 条（本地 / 源机 / GitHub），Windows 默认 GitHub | 1 条：源机 |
| 部署配置文件 | `deploy.conf` + 三个脚本各自解析 | 无；各脚本开头一块 |
| 升级 ffmpeg 步骤 | 改 URL → 清 SHA → build → 回填 SHA → 再 build | 改 URL → 删旧包 → build → 同步源机 |
| torch 版本出现处 | `deploy.conf` + `requirements-cpu.txt` | `build.sh`、`requirements/gpu.txt` |

**自测结果**

| 项 | 结果 |
|----|------|
| `bash -n install.sh` / `bash -n build.sh` | 语法通过 |
| `pip install --dry-run -r requirements/{prod,gpu,ppu}.txt`（清华主索引） | 三份均解析通过；`gpu.txt` 的 `torch==2.8.0+cu128` 从 nju cu128 镜像命中 |
| `git grep deploy.conf`（排除 docs/update） | 仅剩两处解释「已删」的注释 |
| **真机完整 `install.sh` / `install.ps1`** | **未跑**——见遗留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 两个 install 脚本改后未在真机跑过 | 路径/变量名写错会在装物料那步显式失败，不静默 | 下次上机部署时跑一次完整流程；Windows 侧本机可直接跑 `install.ps1` 验证 |
| 源机成为唯一来源，无 fallback | 源机不在线或端口未放通，install 直接失败 | 已写进 DEPLOYMENT §三；本就是「最保险」这一决策的代价 |
| torch 版本两处手动同步 | 漏改一处会导致 Windows 开发机与生产版本不一致 | `build.sh` 与 `gpu.txt` 头注释互相指路；人明确不要为此再拆文件 |
