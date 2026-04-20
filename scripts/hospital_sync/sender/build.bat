@echo off
chcp 65001 >nul
setlocal

echo ============================================================
echo  CleanSight SyncAgent - 构建 64-bit 可执行文件
echo ============================================================
echo.

:: ─── 检查 Python ───────────────────────────────────────────────────────────────
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请确认已安装 Python 3.9+ 并加入 PATH
    pause & exit /b 1
)

for /f "tokens=*" %%v in ('python -c "import sys; print(sys.version)"') do set PY_VER=%%v
echo [OK] Python: %PY_VER%

:: ─── 检查私钥存在 ──────────────────────────────────────────────────────────────
if not exist "hospital_sync_key" (
    echo [错误] 未找到私钥文件 hospital_sync_key，打包前请将其放在此目录
    pause & exit /b 1
)
echo [OK] 私钥文件已就绪

:: ─── 检查/安装依赖 ─────────────────────────────────────────────────────────────
echo.
echo [步骤 1/3] 安装依赖...
python -m pip install --quiet --upgrade pyinstaller paramiko pyodbc
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败
    pause & exit /b 1
)
echo [OK] 依赖就绪

:: ─── PyInstaller 打包 ──────────────────────────────────────────────────────────
echo.
echo [步骤 2/3] 打包中（--onefile 64-bit，私钥内嵌）...

:: hospital_sync_key 通过 --add-data 内嵌进 exe，运行时从 sys._MEIPASS 读取
:: config.ini 保持外部（医院 IT 自行填写 DB 密码），不打包进 exe
pyinstaller ^
    --onefile ^
    --name sync_agent ^
    --add-data "hospital_sync_key;." ^
    --hidden-import paramiko ^
    --hidden-import pyodbc ^
    --hidden-import cryptography ^
    --hidden-import cryptography.hazmat.primitives.asymmetric.ed25519 ^
    --hidden-import cryptography.hazmat.primitives.asymmetric.rsa ^
    --hidden-import cryptography.hazmat.primitives.asymmetric.ec ^
    --hidden-import cryptography.hazmat.backends.openssl ^
    --log-level WARN ^
    sync_agent.py

if %errorlevel% neq 0 (
    echo [错误] PyInstaller 打包失败，请查看上方输出
    pause & exit /b 1
)
echo [OK] 打包完成

:: ─── 整理发布目录 ──────────────────────────────────────────────────────────────
echo.
echo [步骤 3/3] 整理发布目录...

if not exist "release" mkdir release

copy /y "dist\sync_agent.exe"    "release\sync_agent.exe"    >nul
copy /y "config.ini.example"     "release\config.ini.example" >nul
copy /y "setup.bat"              "release\setup.bat"          >nul

:: 确认私钥未混入发布目录
if exist "release\hospital_sync_key" del /f /q "release\hospital_sync_key"

echo.
echo ============================================================
echo  构建完成！发布目录 release\ 包含：
echo    sync_agent.exe      - 主程序（私钥已内嵌，无明文暴露）
echo    config.ini.example  - 配置模板（让医院 IT 填写 DB 密码后重命名为 config.ini）
echo    setup.bat           - 部署脚本
echo.
echo  私钥已打包进 exe，无需单独分发。
echo  【注意】请勿将此 release\ 目录上传到公开代码仓库。
echo ============================================================
echo.
pause
