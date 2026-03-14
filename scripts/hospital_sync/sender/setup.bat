@echo off
chcp 65001 >nul
setlocal

:: ─── 安装目录（固定，config.ini 里的路径与此一致）───────────────────────────
set INSTALL_DIR=C:\hospital_sync

echo ============================================================
echo  CleanSight 医院数据同步 - 部署脚本
echo ============================================================
echo.

:: ─── 检查管理员权限（注册任务计划需要）──────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 请以管理员身份运行此脚本
    echo 右键 setup.bat → 以管理员身份运行
    pause
    exit /b 1
)

:: ─── 创建安装目录 ─────────────────────────────────────────────────────────────
if not exist "%INSTALL_DIR%" (
    mkdir "%INSTALL_DIR%"
    echo [OK] 创建目录 %INSTALL_DIR%
) else (
    echo [OK] 目录已存在 %INSTALL_DIR%
)

:: ─── 复制文件 ─────────────────────────────────────────────────────────────────
set SCRIPT_DIR=%~dp0

copy /y "%SCRIPT_DIR%sync_agent.exe"      "%INSTALL_DIR%\sync_agent.exe"      >nul
copy /y "%SCRIPT_DIR%config.ini"          "%INSTALL_DIR%\config.ini"          >nul
copy /y "%SCRIPT_DIR%hospital_sync_key"   "%INSTALL_DIR%\hospital_sync_key"   >nul
echo [OK] 文件复制完成

:: ─── 限制私钥文件访问权限 ────────────────────────────────────────────────────
icacls "%INSTALL_DIR%\hospital_sync_key" /inheritance:r /grant:r "%USERNAME%:R" >nul
echo [OK] 私钥权限已收紧

:: ─── 注册 Windows 任务计划（每天 06:00 / 12:00 / 18:00）────────────────────
set EXE="%INSTALL_DIR%\sync_agent.exe"
set TASK_PREFIX=HospitalSync

schtasks /delete /tn "%TASK_PREFIX%_06" /f >nul 2>&1
schtasks /delete /tn "%TASK_PREFIX%_12" /f >nul 2>&1
schtasks /delete /tn "%TASK_PREFIX%_18" /f >nul 2>&1

schtasks /create /tn "%TASK_PREFIX%_06" /tr %EXE% /sc daily /st 06:00 /ru SYSTEM /f >nul
schtasks /create /tn "%TASK_PREFIX%_12" /tr %EXE% /sc daily /st 12:00 /ru SYSTEM /f >nul
schtasks /create /tn "%TASK_PREFIX%_18" /tr %EXE% /sc daily /st 18:00 /ru SYSTEM /f >nul
echo [OK] 任务计划已注册（06:00 / 12:00 / 18:00）

:: ─── 立即试跑一次 ─────────────────────────────────────────────────────────────
echo.
echo [运行] 正在执行首次同步测试，请稍候...
"%INSTALL_DIR%\sync_agent.exe"
if %errorlevel% equ 0 (
    echo [OK] 首次同步完成，查看日志：%INSTALL_DIR%\sync.log
) else (
    echo [警告] 同步退出码 %errorlevel%，请查看日志：%INSTALL_DIR%\sync.log
)

echo.
echo ============================================================
echo  部署完成
echo  日志文件：%INSTALL_DIR%\sync.log
echo  卸载任务：schtasks /delete /tn HospitalSync_06 /f
echo            schtasks /delete /tn HospitalSync_12 /f
echo            schtasks /delete /tn HospitalSync_18 /f
echo ============================================================
pause
