# Windows NVIDIA 预检

使用 PowerShell，在目标项目根目录执行。

```powershell
python -V
python -c "import sys, ensurepip; assert (3,10) <= sys.version_info[:2] <= (3,12)"
nvidia-smi
```

需要 Windows x64、Python 3.10–3.12、可访问的 NVIDIA 设备和可用磁盘空间。若 PATH 默认 Python 超出范围，用 `py -0p` 查找已有解释器并选择正确版本，创建环境时显式调用它。

已有 `.venv` 执行 `& .\.venv\Scripts\python.exe -V` 并检查 `.venv/pyvenv.cfg`，不接受继承系统包的 NVIDIA 环境。不要复制 Linux 虚拟环境。

随后执行 [共用 NVIDIA 安装](nvidia.md)。PowerShell 中每次原生命令执行后检查 `$LASTEXITCODE`，非零则停止；`$ErrorActionPreference = 'Stop'` 不替代该检查。
