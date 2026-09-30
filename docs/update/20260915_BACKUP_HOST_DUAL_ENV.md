# 阿里 PPU 备用机部署 prod + test 双环境（端口重分配 + 推理卡可配置）

> **变更状态**：代码改动已落地、备用机环境已就位；**服务尚未启动、端到端未验证**（2026-09-15）
> **知识库**：无需沉淀（单机双环境部署过程，归 docs/DEPLOYMENT.md；其中端口分配规则本身已随 [20260914_PORT_SINGLE_SOURCE](20260914_PORT_SINGLE_SOURCE.md) 进 SERVICE_CONFIG.md）

## 概述

在阿里 PPU 备用机 `8.130.213.80`（SSH `20085`）上以**两个独立目录**并存 prod 与 test 两套环境。
该机对外只有 `20016-20020` 五个口可用，容不下仓库默认端口，也容不下「test 整体 +100」的偏移方案，
故两份副本各持一份 `start_backend.sh`、各自声明绝对端口、偏移恒为 0。

顺带修掉一个既有缺陷：推理子进程的 `CUDA_VISIBLE_DEVICES` 恒为硬编码 `"0"`，
外层 shell 设什么都无效 —— 共享卡的机器上无法锁卡。

## 变更背景

- **触发来源**：4090 主力机不可用时需要备用机顶上；一台机器要同时承载 prod 与 test。
- **承接** [20260912_PPU_PLATFORM_DEPS.md](20260912_PPU_PLATFORM_DEPS.md)（PPU 依赖与机器事实）
  与 [20260914_PORT_SINGLE_SOURCE.md](20260914_PORT_SINGLE_SOURCE.md)（端口收敛到启动脚本）。

### 端口现状实测（2026-09-15，逐口起监听 + 公网回连，非推断）

```
对外 1:1 NAT 可达   20016 ✅  20017 ✅  20018 ✅  20019 ✅  20020 ✅
不可达              20021 ✗   20023 ✗
已被占用            20010-20015(nginx 等)  20022(sshd)  20000/20001
本机 loopback 空闲   18004 18104 / 8002 8003 8102 8103
```

**推翻 09-12 记录的一条结论**：当时写「该机对外只暴露 SSH，RTSP/HTTP 外部均不可达，是部署前置阻塞项」——
平台此后开放了 `20010-20020` 段并做 1:1 映射，该阻塞项已解除。

`20022` 一度被拟作 test 端口，实为 sshd 监听口，且在映射窗口之外，不可用。

## 方案详情

### 全景：两个目录 → 两份脚本 → 两套端口 + 两个库

```text
/root/CleanSightBackend          (prod)  ./start_backend.sh prod → .env
  └─ 20018 后端 / 20019 RTSP / 18004 MediaMTX / 8002,8003 RTP,RTCP

/root/CleanSightBackend-test     (test)  ./start_backend.sh test → .env.test
  └─ 20016 后端 / 20017 RTSP / 18104 MediaMTX / 8102,8103 RTP,RTCP
```

对外两口的外部端口号与内部完全相同，满足 `_rewrite_rtsp_url` 的 1:1 NAT 前提。

### 1. 为什么不用「test +100」而拆目录

上游的 +100 偏移是为「同目录、同一份脚本跑两个环境」设计的。在此机上 prod 取 20018 则 test 落到 20116，
不在映射窗口内。拆成两个目录后每份脚本只服务一个环境，端口写成绝对值、`OFFSET=0`，
两脚本头部各标注了本副本是哪个环境、另一个在哪。

副作用：两份副本的 `start_backend.sh` 各自带 git 本地 diff（与 09-14 记录中「部署机改脚本留本地 diff」的预期一致）。

### 2. `cuda_device` 从硬编码变为可配置（本次唯一的仓库代码改动）

`RemoteInferProxy.__init__` 早有 `cuda_device` kwarg，但 `DetectionService` 从未传值，恒取默认 `"0"`。
而 `stage_worker.run_stages` 在 `import torch` 之前**无条件**执行
`os.environ["CUDA_VISIBLE_DEVICES"] = cuda_device`，把外层 shell 的值覆盖掉。

**实测证据**（PPU 卡 Bus-Id：0=05:00.0 1=06:00.0 2=0B:00.0 3=0C:00.0）：

| 场景 | 拿到的卡 |
|------|---------|
| 外层 `CUDA_VISIBLE_DEVICES=3`，进程内不改写 | bus-id 12 (0x0C) = 卡 3 ✅ |
| 外层 `=3`，进程内按 `stage_worker` 改写为 `"0"`（实际代码路径） | bus-id 5 (0x05) = 卡 0 ❌ |

即共享卡机器上**无法通过启动命令锁卡**。改动：

```
app/settings.py   + cuda_device: str = "0"   # env: CLEANSIGHT_CUDA_DEVICE
service.py:93     RemoteInferProxy(..., cuda_device=settings.cuda_device)
.env.example      + 注释：外层 shell 管不到子进程，此处是唯一旋钮
```

**不改 `stage_worker.py` 让它尊重外层变量**：改成 `setdefault` 后，CPU-only 测试显式传的
`cuda_device=""` 会被 shell 残留值压过、静默跑上 GPU。显式 kwarg 必须保持权威。

本机两个环境均锁**卡 3**（`.env` 与 `.env.test` 各写 `CLEANSIGHT_CUDA_DEVICE=3`）。

### 3. `.env.dev` 占位文件（两副本各一份）

`pytest` 与 `app.settings` 默认按 `CLEANSIGHT_ENV=dev` 加载 `.env.dev`，缺失则在构造 `Settings()` 时
抛 pydantic `ValidationError`（发生在连库之前，无副作用，但测试跑不起来）。该机上此文件 09-12 建过、后已丢失。

补的这份所有外部地址一律指向 RFC 2606 保留域名 `.invalid`（DNS 永不解析），
使「以 dev 配置跑任何测试」都不可能误连真实库或误发告警。

### 4. 刻意不动的项

- **`.env` / `.env.test` 的库指向保持现状**：该机上 `.env`(prod) → `aidkdbtest`/`:8882`，
  `.env.test` → `aidkdb`/`:8881`，与本地仓库正好相反。经确认是**有意为之**。
  ⚠️ 后果：在此机上跑 `./start_backend.sh test` 或其集成测试会写**真实生产库**并发**真实告警**。
- **test 副本的 `database/9002/2`**：从 prod 副本原样复制而来的历史存储产物，未清理。
- **半精度**：09-12 记录建议在 PPU 上关闭 fp16，但代码库中并无半精度开关（`grep -i half|fp16` 无命中），无可操作项。

### 5. 仓库侧：test 偏移 +100 → +2（追加改动，2026-09-16）

与备用机无关的一处主线调整：`+100` 跨度过大，收窄为 `+2`。两个启动脚本各改一处 offset，
派生值实测如下：

| 环境 | 后端 | 网关 RTSP | MediaMTX RTSP | RTP / RTCP |
|------|------|-----------|---------------|-----------|
| dev / prod | 8000 | 8004 | 18004 | 8002 / 8003 |
| test（+2） | 8002 | 8006 | 18006 | 8004 / 8005 |

**代价：两套端口从此交错**。`test` 后端 TCP `8002` 与 `prod` RTP UDP `8002` 同号，
`test` RTP UDP `8004` 与 `prod` 网关 TCP `8004` 同号 —— 只因 TCP 与 UDP 是两个独立命名空间，
实际绑定才不冲突。`+100` 时两套是分离的号段，肉眼可辨；`+2` 之后必须按协议分辨。
两个脚本的端口块、`DEPLOYMENT.md` 端口段都补了这条警告：改基准值或新增端口前要按协议重算两套集合。

> 若日后觉得这个交错难维护，`+10` 是同样紧凑但不交错的选择（test → 8010/8014/18014/8012/8013）。

**自测**：`sh` 抽出端口块跑 dev/prod/test 三档，派生值与上表一致；`ps1` 经
`[Parser]::ParseFile` 校验 0 错误，并已把本次编辑引入的 LF 行统一回 CRLF
（该文件无 BOM + 中文注释，PS 5.1 按 GBK 读时 LF 会导致解析失败，见
[20260914_PORT_SINGLE_SOURCE.md](20260914_PORT_SINGLE_SOURCE.md) 遗留风险末条）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 备用机可承载环境数 | 1（端口与仓库默认冲突，8000 被占） | 2（prod + test 并存，端口互不重叠） |
| 推理卡选择 | 硬编码卡 0，无任何配置入口 | `CLEANSIGHT_CUDA_DEVICE`，按环境可配 |
| 该机 `pytest` | 跑不起来（缺 `.env.dev`） | 可跑，且配置不可能碰真实库 |

**已验证**

| 项 | 结果 |
|----|------|
| test 副本 venv 重建（`--system-site-packages` + `requirements-ppu.txt`） | `BUILD_EXIT=0` |
| test 副本环境冒烟（torch 2.10.0+ppu / cv2 GUI:NONE / numpy 2.3.5 / PPU matmul） | 全通 |
| test 副本 `pytest tests/` | **738 passed** |
| 本地 `pytest -k "infer or detection or settings"`（`cuda_device` 改动后） | 29 passed |
| 20016-20020 对外 1:1 可达 | 逐口实测通过，临时监听已全部清除 |

**未验证（待执行）**：两个环境均未启动；`/health/status`、网关双口在听、端到端 `test_single_client.py` 全部未跑。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `.env.test` 指向真实生产库 | 在此机跑 test 或其集成测试 = 写生产库 + 发真实告警 | 已确认为有意配置；启动前须知情 |
| 仅剩 1 个空闲对外口（20020） | 若后续要开 HLS/metrics 对外口则不够 | 需向平台申请扩段 |
| 两份 `start_backend.sh` 各带本地 diff | `git pull` 可能冲突；改了一边忘另一边 → 端口漂移 | 脚本头部已标注本副本环境与另一副本路径 |
| `cuda_device` 改动尚未提交进仓库 | 备用机上是 scp 的本地 diff，`git pull` 会冲突或丢失 | 需在主线提交后由部署机拉取 |
| 该机 `pkill -f "app.main"` 会误杀邻居 | **别的租户**在跑 `gunicorn app.main:app`（pid 1445663）与 `uvicorn --port 8000` | 停服务只能按端口号或 `/proc/PID/cwd` 精确匹配，禁用 deploy skill 里那条通用 `pkill` |
| 本次操作留下的一处污染（已清） | 冒烟测试漏设 `YOLO_CONFIG_DIR`，ultralytics 建了全局 `/root/.config/Ultralytics/settings.json`，把 `weights_dir`/`runs_dir` 指向本项目目录，会劫持同机其他租户的 ultralytics 任务 | 已删除文件与目录，机器恢复原状；后续每条命令固定带 `YOLO_CONFIG_DIR` |
