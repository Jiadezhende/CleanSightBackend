# FFmpeg 手动安装指南（Windows）

## 快速安装步骤

### 1. 下载 FFmpeg

访问以下链接下载预编译的 Windows 版本：
- **官方下载页**：https://ffmpeg.org/download.html#build-windows
- **推荐：gyan.dev 构建**：https://www.gyan.dev/ffmpeg/builds/

**选择版本：**
- 下载 `ffmpeg-release-essentials.zip`（约 80MB，包含基本功能）
- 或 `ffmpeg-release-full.zip`（约 150MB，包含所有编解码器）

### 2. 解压到项目目录

```powershell
# 假设下载到 Downloads 文件夹
# 解压到项目根目录
cd E:\ywc_college\junior1\本科生课题\src\CleanSightBackend

# 创建 ffmpeg 文件夹
mkdir ffmpeg

# 解压后文件结构应该是：
# CleanSightBackend/
#   ffmpeg/
#     bin/
#       ffmpeg.exe
#       ffprobe.exe
#       ffplay.exe
#     doc/
#     presets/
```

### 3. 添加到 PATH（临时方法）

**在当前 PowerShell 会话中临时添加：**

```powershell
# 添加 ffmpeg 到当前会话的 PATH
$env:Path += ";E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\ffmpeg\bin"

# 验证
ffmpeg -version
```

**每次打开新终端都需要运行上述命令**

### 4. 添加到 PATH（永久方法）

**选项 A: 用户环境变量（推荐，无需管理员）**

1. 按 `Win + X`，选择"系统"
2. 点击"高级系统设置"
3. 点击"环境变量"
4. 在"用户变量"部分，选择 `Path`，点击"编辑"
5. 点击"新建"，添加：
   ```
   E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\ffmpeg\bin
   ```
6. 点击"确定"保存
7. **重启所有 PowerShell 窗口**
8. 验证：`ffmpeg -version`

**选项 B: PowerShell 配置文件（自动加载）**

```powershell
# 编辑 PowerShell 配置文件
notepad $PROFILE

# 如果文件不存在，先创建
if (!(Test-Path -Path $PROFILE)) {
    New-Item -ItemType File -Path $PROFILE -Force
}

# 在打开的记事本中添加以下内容：
$env:Path += ";E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\ffmpeg\bin"

# 保存并关闭
# 重新加载配置
. $PROFILE

# 验证
ffmpeg -version
```

### 5. 验证安装

```powershell
# 检查版本
ffmpeg -version

# 检查编解码器
ffmpeg -codecs | Select-String "h264"
ffmpeg -codecs | Select-String "aac"

# 应该看到类似输出：
# ffmpeg version N-XXXXX Copyright (c) 2000-2024 the FFmpeg developers
```

---

## 快速测试（安装后）

```powershell
# 进入测试目录
cd E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\test

# 推流测试视频
ffmpeg -re -i test_video.mp4 -c:v libx264 -preset veryfast -tune zerolatency -f flv rtmp://localhost:1935/live/test
```

---

## 故障排查

### 问题 1: "ffmpeg" 仍然无法识别

**原因：** PATH 未生效

**解决：**
```powershell
# 1. 检查 PATH
$env:Path -split ";" | Select-String "ffmpeg"

# 2. 如果没有输出，手动添加
$env:Path += ";E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\ffmpeg\bin"

# 3. 验证
ffmpeg -version
```

### 问题 2: 找不到 DLL 文件

**错误：** 缺少 `avcodec-XX.dll` 等文件

**解决：**
- 确保解压了完整的 ffmpeg 包
- 所有 DLL 文件应该在 `ffmpeg/bin/` 目录中
- 重新下载并解压

### 问题 3: 权限错误

**错误：** 无法执行 ffmpeg.exe

**解决：**
```powershell
# 检查文件是否存在
Test-Path "E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\ffmpeg\bin\ffmpeg.exe"

# 尝试直接运行（使用完整路径）
& "E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\ffmpeg\bin\ffmpeg.exe" -version
```

---

## 一键安装脚本

创建 `install_ffmpeg.ps1`：

```powershell
# install_ffmpeg.ps1
# FFmpeg 自动下载和配置脚本

Write-Host "=== FFmpeg 自动安装脚本 ===" -ForegroundColor Green

$ProjectRoot = "E:\ywc_college\junior1\本科生课题\src\CleanSightBackend"
$FFmpegDir = Join-Path $ProjectRoot "ffmpeg"
$FFmpegBin = Join-Path $FFmpegDir "bin"

# 检查是否已安装
if (Test-Path "$FFmpegBin\ffmpeg.exe") {
    Write-Host "✓ FFmpeg 已安装在: $FFmpegBin" -ForegroundColor Green
    & "$FFmpegBin\ffmpeg.exe" -version | Select-Object -First 1
    
    # 添加到 PATH
    if ($env:Path -notlike "*$FFmpegBin*") {
        Write-Host "`n添加到当前会话 PATH..." -ForegroundColor Yellow
        $env:Path += ";$FFmpegBin"
        Write-Host "✓ 已添加到 PATH" -ForegroundColor Green
    }
    
    exit 0
}

Write-Host "`n❌ FFmpeg 未找到" -ForegroundColor Red
Write-Host "`n请手动安装 FFmpeg：" -ForegroundColor Yellow
Write-Host "1. 下载：https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
Write-Host "2. 解压到：$FFmpegDir"
Write-Host "3. 确保 ffmpeg.exe 在：$FFmpegBin\ffmpeg.exe"
Write-Host "4. 重新运行此脚本"

# 可选：尝试使用 Chocolatey 安装
Write-Host "`n或使用 Chocolatey 安装（需要管理员权限）：" -ForegroundColor Cyan
Write-Host "choco install ffmpeg -y" -ForegroundColor White
```

使用脚本：
```powershell
.\install_ffmpeg.ps1
```

---

## 推荐下载链接

**官方来源：**
1. **gyan.dev**（推荐）
   - https://www.gyan.dev/ffmpeg/builds/
   - 下载 `ffmpeg-release-essentials.zip`

2. **BtbN GitHub Releases**
   - https://github.com/BtbN/FFmpeg-Builds/releases
   - 下载 `ffmpeg-master-latest-win64-gpl.zip`

3. **FFmpeg 官方**
   - https://ffmpeg.org/download.html#build-windows
   - 选择 Windows 构建链接

---

## 总结

✅ **安装完成后，验证：**
```powershell
ffmpeg -version
ffmpeg -codecs | Select-String "h264"
```

✅ **测试推流：**
```powershell
cd test
ffmpeg -re -i test_video.mp4 -c:v libx264 -f flv rtmp://localhost:1935/live/test
```

🚀 **准备就绪后，继续运行集成测试！**
