# NVIDIA 共用安装

仅在 NVIDIA 预检通过后执行。PPU 使用自己的 reference。

## 创建或复用环境

已有环境按入口要求检查并复用。仅在 `.venv` 不存在时创建。

Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell：

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
```

若选择的解释器不在默认命令上，以上创建命令改用预检确认的 Python 路径。之后 `python` 必须指向该项目 `.venv`。

## Python 包

完整依赖与 CUDA 构建版本在 `requirements/gpu.txt` 声明，本节提供下载来源。已有版本匹配时 pip 会复用。

```bash
python -m pip install -r requirements/gpu.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://mirror.nju.edu.cn/pytorch/whl/cu128
```

CUDA 镜像不可用时可改用 `https://download.pytorch.org/whl/cu128`。普通镜像不可用时可选择阿里镜像或官方 PyPI。requirements 中明确的 `+cu128` 构建约束要求获取对应 CUDA wheel。保留版本约束，根据错误更换来源；Python 3.10／3.11／3.12 均由 pip 选择匹配 wheel。

随后执行 [OpenCV 检查、必要修复与环境验证](common.md)，准备二进制后完成以下设备检查。

## 设备计算验证

```bash
python -c "import torch; assert torch.cuda.is_available(), 'CUDA unavailable'; layer=torch.nn.Conv2d(3,8,3).cuda(); output=layer(torch.randn(1,3,64,64,device='cuda')); torch.cuda.synchronize(); assert torch.isfinite(output).all().item(); print('NVIDIA convolution OK')"
```

使用可用且获准使用的设备。共享 GPU 时按现场情况给此命令设置设备选择，不修改应用运行配置。此验证不加载模型权重。
