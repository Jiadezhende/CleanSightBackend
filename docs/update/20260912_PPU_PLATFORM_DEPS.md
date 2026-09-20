# 阿里 PPU 平台依赖清单验证（备用机 cleansight-copy）

知识库：无需沉淀（备用机依赖清单属部署物料，归 docs/DEPLOYMENT.md；KB 记系统事实，不记单机环境）
日期：2026-09-12

## 结论

备用机 `8.130.213.80:20085` 不是 NVIDIA 机器，是 **4× 阿里 PPU-ZW810E**（CUDA 兼容层 `/usr/local/PPU_SDK/CUDA_SDK`）。
主线 `install.sh` 路径在此不可用，改用新增的 [requirements-ppu.txt](../../requirements-ppu.txt)，**28 项验证全通**（干净 venv 重装复现）。

YOLO 在 PPU 上**数值正确**，不是「只是不报错」：同图同权重对 CPU 基准，检出数一致、类别一致、置信度最大偏差 `4.35e-4`、框坐标最大偏差 `0.019 px`。

## 机器事实

| 项 | 值 |
|----|----|
| 形态 | 阿里云 K8s Pod（`/proc/1/cgroup` 为 `kubepods/burstable/...`），44 核 / 440G 内存 / 盘余 337G |
| 加速卡 | 4× PPU-ZW810E，96GB/张，sm8.0、64 SM；设备节点 `/dev/alixpu_ppu0..3` |
| OS / Python | Ubuntu 24.04.2 LTS / Python 3.12.3（**无 3.10**，系统无 conda） |
| torch | `2.10.0+ppu2.1.0`（配套 torchvision `0.25.0+ppu2.1.0`、torchaudio 同版）预装于系统 site-packages |
| numpy | 2.3.5（PPU torch 按 numpy 2.x 编译） |
| cv2 | 系统装了 opencv-python 4.11.0.86（**QT5 GUI 版**）与 headless 4.13.0.92，实际解析到 GUI 版 |
| ffmpeg | 系统 `/usr/bin/ffmpeg` 6.1.1-3ubuntu5（与 deploy.conf 钉的 n7.1.4 不同版） |
| mediamtx | 未装 |

## 依赖清单的两处关键差异

主线 `requirements.txt` 与 `requirements-ppu.txt` 只差两项，但都属于「装了就废」：

1. **numpy 不可锁 1.26.4**。主线为修 opencv 强制降级到 1.26.4；PPU torch 按 numpy 2.x 编译，降级会崩。PPU 版直接继承系统 2.3.5，清单里不列 numpy。
2. **torch/torchvision 不可由 pip 装**。`deploy.conf` 的 `TORCH_PKGS="torch==2.8.0 torchvision==0.23.0"` 是官方 CUDA wheel，链接 NVIDIA libcuda；PPU 版只有 cp312、随系统预装。因此 venv **必须** `--system-site-packages`。

反过来，`opencv-python-headless<4.12.0` 在 PPU 版里**必须显式列出**（主线同样有）：系统那份 cv2 是 `GUI: QT5` 构建，需要在 venv 内 shadow 成 `GUI: NONE`。

## 验证方式与结果

```
source /usr/local/PPU_SDK/envsetup.sh
/usr/local/bin/python3 -m venv --system-site-packages venv-clean
venv-clean/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements-ppu.txt
CUDA_VISIBLE_DEVICES=3 venv-clean/bin/python verify_deps.py     # 28 项全通
```

覆盖：torch PPU matmul、cv2 headless 构建断言 + imencode、18 个包的 import+版本、
YOLO11n 推理 PPU vs CPU 数值比对、ByteTrack 跟踪（验 lap）、FastAPI TestClient 起服务、
SQLAlchemy+psycopg2 查询、websockets 收发、uvicorn[standard] 的 uvloop/httptools/watchfiles。

实测性能（YOLO11n 640×640，单卡）：**fp32 6.3 ms/帧**。

## 踩坑（部署到此类机器必看）

1. **不 source `envsetup.sh` 会直接 abort**。PPU 运行时需 `PPU_SDK`/`CUDA_HOME`/`LD_LIBRARY_PATH` 等；
   缺失时简单 matmul 能跑，但 YOLO 卷积走 RTC 编译即 `Both PPU_SDK and PPU_HOME are not exist` 崩溃（exit 134）。
2. **fp16 比 fp32 慢**（7.9 ms vs 6.3 ms）。此卡 FP16 路径无 tensor-core 级加速，
   NVIDIA 上「开 half 提速」的经验在此是负优化，配置里要关掉半精度。
3. **首次冷启要现场编译内核**。`/usr/local/PPU_SDK/rtccache/` 未命中时每个卷积配置约编译 1.4 秒，
   刷大量 `No cache file exist` warning。缓存全局共享，仅首次付代价，但与「预热约 2s」的预期差一个数量级。
4. **清华 pypi 源对部分 wheel 返回 403**（实测 ultralytics）。改用 `https://mirrors.aliyun.com/pypi/simple/`，
   该机在阿里内网也更快。
5. **这是共享机器**，同时跑着别的项目（ComfyUI / CyberVerse / TTS×3 / InfiniteTalk，均已连续运行 1–4 天）。
   在其上做任何 GPU 测试都应 `CUDA_VISIBLE_DEVICES` 锁单卡 + 脚本内断言可见设备数，避免干扰。

## 端口核查（2026-09-12 补）

CleanSight 实际占用的端口集合（来自 `mediamtx/mediamtx.yml` + `mediamtx_gateway/config.ini` + `CLEANSIGHT_PORT`）：

| 端口 | 协议 | 绑定 | 用途 | 该机状态 |
|------|------|------|------|---------|
| 8000 | TCP | 0.0.0.0 | 后端 HTTP/WS | **冲突** |
| 8004 | TCP | 0.0.0.0 | 网关 RTSP 代理 | 空闲 |
| 18004 | TCP | 127.0.0.1 | MediaMTX RTSP | 空闲 |
| 8002 | UDP | 127.0.0.1 | RTP | 空闲 |
| 8003 | UDP | 127.0.0.1 | RTCP | 空闲 |

mediamtx.yml 中 api/metrics/pprof/playback/rtmp/hls/webrtc/srt 均为 `no`、`rtspEncryption: "no"`，
故 9997/9998/9999/9996/1935/8888/8889/8890/8322 都不监听 —— 其中 8888、8889 在该机已被占用，
但因功能关闭而**不构成冲突**，启用前需重新评估。prometheus 指标走 app 路由（`generate_latest`），不另开端口。

### 冲突 1：8000 被其他容器的 uvicorn 占用，无法回收

`127.0.0.1:8000` 有 FastAPI 服务在跑（响应头 `server: uvicorn` + `x-process-time`，但 `/health/status` 返回 404，非 CleanSight）。
通过 `/proc/net/tcp` 的 socket inode 反查，**本 PID 命名空间内无匹配进程** —— 属于共享同一 netns 的其他容器，
我们既看不到也不该动它。处置：改 `CLEANSIGHT_PORT`，建议 **8010**（该机空闲，且不在已用段内）。

该机已占用 TCP：`22 80 3000 3080 4096 5000 5432 5433 5434 7002 7681 8000 8015 8016 8018 8019 8080 8082
8086 8088 8090 8098 8418 8443 8887 8888 8889 9303 9501 9601 9806 15051 15173 18080 20000-20001 20010-20015
20022` + 若干高位临时端口；UDP：`323 4791`。

### 冲突 2（更严重）：该机对外只暴露 SSH，RTSP/HTTP 外部均不可达

从外部逐个探测 `8.130.213.80`：

```
:20085  可连通   ← SSH（唯一对外入口）
:20000  不通     ← ComfyUI，容器内监听 0.0.0.0 仍不可达
:8000   不通
:8004   不通     ← 摄像头推流入口
:18004  不通
:8418   不通
```

即容器内 `0.0.0.0` 监听**不等于**外部可达，Pod 只映射了 SSH。影响：
摄像头无法推流到网关 8004，外部客户端也调不到后端 API/WS —— 端到端链路在此机上跑不通，
只能在容器内自环（`integration_tests/test_single_client.py` 推 `rtsp://127.0.0.1:8004` 这种自测仍可跑）。

**这是部署到该机的前置阻塞项**，需向平台方确认能否为 8004/后端端口开端口映射或 ingress。

## 环境落地（2026-09-12 补）

仓库已 clone 到该机 `/root/CleanSightBackend`（分支 `dev`），环境配置完成，**`pytest tests/` 672 全通、0 skip**。

```bash
cd /root/CleanSightBackend
source /usr/local/PPU_SDK/envsetup.sh          # 每次必须，否则 PPU 内核编译 abort
export CUDA_VISIBLE_DEVICES=3                  # 共享机器，锁单卡避免干扰他人
export YOLO_CONFIG_DIR=$PWD/.ultralytics
.venv/bin/python -m pytest tests/ -q           # 672 passed in 44s
```

落地内容：

- **`.venv`**：`/usr/local/bin/python3 -m venv --system-site-packages .venv`，按 `requirements-ppu.txt` 安装。
- **`.env.dev`**：占位配置，DB/告警地址一律指向 RFC 2606 保留域名 `.invalid`（DNS 永不解析），
  保证跑测试不可能误连真实库或误发告警；另含 `CLEANSIGHT_PORT=8010`（避开被占的 8000）。
- **物料**：从分发机 `http://49.234.120.241:8088` 拉取，按 `deploy.conf` 钉版 SHA256 校验通过：
  `ffmpeg n7.1.4-7-gadcf20da26` → `.ffmpeg/`，`mediamtx v1.15.5` → `mediamtx/mediamtx`。
  **未跑 `install.sh`** —— 它会重建 venv 并装官方 CUDA 版 torch，会毁掉 PPU 环境；只复刻了其中 `[2]`/`[3]` 物料两段。

补物料前有 8 个用例因「需要 cv2 与项目自带 ffmpeg」被 skip（`test_storage_hls.py` ×7、`test_recording_service.py` ×1），
钉版 ffmpeg 就位后全部转为通过。

### 顺带发现：`start_backend.sh` 在 git 里没有执行位

`-rw-r--r--`，`git clone` 下来也是如此 —— 此前记录在 deploy skill 里的「`git archive` 丢 +x」其实是仓库本身的问题，
每次 clone 都会撞。根治：`git update-index --chmod=+x start_backend.sh`。本机已 `chmod +x` 绕过。

## 未决项

- **仓库里还没有 `requirements-ppu.txt`**：目前是 scp 到该机的，未提交。
- **端口映射未申请**：RTSP 8004 需要 NAT DNAT + 安全组（见上节），未办前接不了真实摄像头。
- **未跑端到端 `test_single_client.py`**：需真实 DB 与告警端点，且涉及写库/发告警，须先与人确认。
