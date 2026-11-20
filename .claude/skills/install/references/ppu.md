# PPU 厂商环境安装

本流程适用于提供 `/usr/local/PPU_SDK` CUDA 兼容层的阿里 PPU。先核实当前平台，厂商镜像可能变化。不要安装 NVIDIA 官方 PyTorch，不向系统 Python 安装项目包。

## 预检

确认 `/dev/alixpu_ppu*`、SDK 初始化文件和平台持久盘。已知实例 `/mnt/workspace` 为持久 POSIX 存储，根盘重建会丢失；实际以目标实例挂载为准。OSS FUSE 不适合本项目随机写缓存和 mp4。

```bash
source /usr/local/PPU_SDK/envsetup.sh
ppu-smi
/usr/local/bin/python3 -V
/usr/local/bin/python3 -c "import sys, torch, torchvision, numpy; assert (3,10) <= sys.version_info[:2] <= (3,12); print(torch.__version__, torchvision.__version__, numpy.__version__); print(torch.__file__, torchvision.__file__, numpy.__file__)"
```

记录实际厂商包路径，确认可用；缺包、版本不兼容或 Python 超范围时停止报告。PPU 的 PyTorch／torchvision 版本跟随厂商，不套用 NVIDIA 的版本。共享设备先按 `ppu-smi` 选择获准使用的卡。

## 项目环境

已有 `.venv` 必须使用当前厂商 Python、带 `include-system-site-packages = true`。检查失败时停止询问，避免覆盖。不复制其他平台的虚拟环境。

仅在不存在时创建：

```bash
/usr/local/bin/python3 -m venv --system-site-packages .venv
```

随后激活并安装。NumPy 约束读取本机厂商版本，保护其可用依赖组合：

```bash
source .venv/bin/activate
np_constraint=$(mktemp)
/usr/local/bin/python3 -c "import numpy; print('numpy==' + numpy.__version__)" > "$np_constraint"
python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -c "$np_constraint" -r requirements/ppu.txt
rm -f "$np_constraint"
```

每条命令检查退出状态，安装失败时停止。按 [共用检查](common.md) 处理 OpenCV，二进制安装复用 Linux 路径。不要因其他系统应用的依赖告警，擅自更新厂商包或整个系统环境。

## 专用验证

先执行共用环境验证，再执行以下命令。每个新 shell 都需要加载 SDK；缺少它可能使卷积内核编译 abort。

```bash
source /usr/local/PPU_SDK/envsetup.sh
python -c "import torch, torchvision, numpy; from pathlib import Path; prefix=Path(__import__('sys').prefix).resolve(); packages=(torch,torchvision,numpy); assert all(prefix not in Path(p.__file__).resolve().parents for p in packages), 'Vendor packages shadowed in venv'; print([(p.__name__,p.__file__) for p in packages]); assert torch.cuda.is_available(); layer=torch.nn.Conv2d(3,8,3).cuda(); output=layer(torch.randn(1,3,64,64,device='cuda')); torch.cuda.synchronize(); assert torch.isfinite(output).all().item(); print('PPU convolution OK')"
```

检查路径应与预检记录的厂商包一致；若系统包位置与预期不同，核查平台镜像，不自行用官方包替换。首次卷积可能编译内核，需要等待其完成。模型、端口、运行角色、服务进程与 `.env` 不在本安装流程内。
