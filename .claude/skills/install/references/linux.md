# Linux NVIDIA 预检

在目标项目根目录执行；以下仅检查环境。

```bash
uname -sm
python3 -V
python3 -c 'import sys, ensurepip; assert (3,10) <= sys.version_info[:2] <= (3,12)'
nvidia-smi
```

需要 Linux x86_64、Python 3.10–3.12、可访问的 NVIDIA 设备、可用磁盘空间，以及 Python 索引和源机的网络访问。没有合适 Python 或系统库时先说明缺项，根据用户权限准备；不安装或替换宿主驱动。

固定容器先确认项目目录位于平台持久盘，系统 Python 与 `.venv` 的解释器仍存在。已有 `.venv` 执行 `.venv/bin/python -V` 并检查 `.venv/pyvenv.cfg`，不接受继承系统包的 NVIDIA 环境。

远程环境仅操作用户指定目标；代码可浅克隆或上传 Git 归档，避免传递本地 `.venv`、配置、模型和业务数据。

随后执行 [共用 NVIDIA 安装](nvidia.md)。
