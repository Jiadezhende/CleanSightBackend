# 共用 OpenCV 处理与依赖验证

使用目标项目 `.venv` 的 Python，命令均在项目根目录执行。

## OpenCV

```bash
python .claude/skills/install/scripts/verify_environment.py --opencv-only
```

检查成功则跳过修复。检查失败先看原因；Python 范围错误应先处理解释器。只有 OpenCV 包冲突、导入失败或 GUI 构建时，执行以下修复：

```bash
python -m pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python opencv-contrib-python-headless
python -m pip install --no-deps --force-reinstall "opencv-python-headless<4.12.0" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

PPU 默认使用 `https://mirrors.aliyun.com/pypi/simple/`。修复只修改项目虚拟环境，禁止修改系统 site-packages。PPU 若健康 headless 来自系统则直接复用。`--no-deps` 保留已经选定的 NumPy，避免修复 OpenCV 时重新解析覆盖它。

## 安装完成检查

FFmpeg／MediaMTX 按入口使用源机归档安装后执行：

```bash
python .claude/skills/install/scripts/verify_environment.py
```

该命令检查关键依赖导入、headless 和两个二进制可执行性，创建项目 `.ultralytics` 供导入使用，不加载应用配置。最后执行平台 reference 中的设备验证。失败时报告具体步骤，深入的依赖审计按故障需要执行。
