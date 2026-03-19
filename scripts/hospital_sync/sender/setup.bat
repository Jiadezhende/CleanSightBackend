@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: ─── 安装目录 ──────────────────────────────────────────────────────────────────
set INSTALL_DIR=C:\hospital_sync

echo ============================================================
echo  CleanSight 医院数据同步 - 部署脚本
echo ============================================================
echo.

:: ─── 检查管理员权限 ────────────────────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 请以管理员身份运行此脚本
    echo 右键 setup.bat → 以管理员身份运行
    pause & exit /b 1
)

:: ─── 检查 exe 存在 ─────────────────────────────────────────────────────────────
set SCRIPT_DIR=%~dp0
if not exist "%SCRIPT_DIR%sync_agent.exe" (
    echo [错误] 未找到 sync_agent.exe，请确认与 setup.bat 在同一目录
    pause & exit /b 1
)

:: ─── 处理 config.ini ──────────────────────────────────────────────────────────
set CONFIG_SRC=%SCRIPT_DIR%config.ini
if not exist "%CONFIG_SRC%" (
    if exist "%SCRIPT_DIR%config.ini.example" (
        echo [提示] 未找到 config.ini，将通过向导生成...
        echo.
        call :CONFIG_WIZARD
    ) else (
        echo [错误] 未找到 config.ini 或 config.ini.example
        pause & exit /b 1
    )
)

:: ─── 创建安装目录 ──────────────────────────────────────────────────────────────
if not exist "%INSTALL_DIR%" (
    mkdir "%INSTALL_DIR%"
    echo [OK] 创建目录 %INSTALL_DIR%
) else (
    echo [OK] 目录已存在 %INSTALL_DIR%
)

:: ─── 复制文件（私钥已内嵌在 exe，无需复制）────────────────────────────────────
copy /y "%SCRIPT_DIR%sync_agent.exe"  "%INSTALL_DIR%\sync_agent.exe"  >nul
copy /y "%CONFIG_SRC%"                "%INSTALL_DIR%\config.ini"       >nul
echo [OK] 文件复制完成

:: ─── 锁定 config.ini 权限（含 DB 密码）───────────────────────────────────────
icacls "%INSTALL_DIR%\config.ini" /inheritance:r /grant:r "%USERNAME%:(R,W)" /grant:r "SYSTEM:(R,W)" >nul
echo [OK] config.ini 权限已收紧（仅当前用户和 SYSTEM 可读）

:: ─── 注册 Windows 任务计划 ────────────────────────────────────────────────────
set EXE="%INSTALL_DIR%\sync_agent.exe"
set TASK_PREFIX=HospitalSync

schtasks /delete /tn "%TASK_PREFIX%_06" /f >nul 2>&1
schtasks /delete /tn "%TASK_PREFIX%_12" /f >nul 2>&1
schtasks /delete /tn "%TASK_PREFIX%_18" /f >nul 2>&1

schtasks /create /tn "%TASK_PREFIX%_06" /tr %EXE% /sc daily /st 06:00 /ru SYSTEM /f >nul
schtasks /create /tn "%TASK_PREFIX%_12" /tr %EXE% /sc daily /st 12:00 /ru SYSTEM /f >nul
schtasks /create /tn "%TASK_PREFIX%_18" /tr %EXE% /sc daily /st 18:00 /ru SYSTEM /f >nul
echo [OK] 任务计划已注册（06:00 / 12:00 / 18:00 每日自动运行）

:: ─── 立即试跑 ─────────────────────────────────────────────────────────────────
echo.
echo [运行] 正在执行首次同步测试，请稍候...
"%INSTALL_DIR%\sync_agent.exe"
if %errorlevel% equ 0 (
    echo [OK] 首次同步完成
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
exit /b 0


:: ══════════════════════════════════════════════════════════════
:CONFIG_WIZARD
:: 交互式生成 config.ini（仅在 config.ini 不存在时调用）
:: ══════════════════════════════════════════════════════════════
echo === SQL Server 连接配置 ===
echo （直接回车使用括号内默认值）
echo.

set /p "DB_SERVER=SQL Server 地址 [localhost]: "
if "!DB_SERVER!"=="" set DB_SERVER=localhost

set /p "DB_NAME=数据库名 [master]: "
if "!DB_NAME!"=="" set DB_NAME=master

set /p "DB_USER=用户名 [sa]: "
if "!DB_USER!"=="" set DB_USER=sa

set /p "DB_PASS=密码: "

set /p "DB_DRIVER=ODBC 驱动名 [ODBC Driver 17 for SQL Server]: "
if "!DB_DRIVER!"=="" set DB_DRIVER=ODBC Driver 17 for SQL Server

echo.
echo 以上信息将写入 %SCRIPT_DIR%config.ini
echo.

(
echo [sqlserver]
echo server   = !DB_SERVER!
echo database = !DB_NAME!
echo username = !DB_USER!
echo password = !DB_PASS!
echo driver   = !DB_DRIVER!
echo.
echo [linked_server]
echo name     = NJXX
echo database = THIS4
echo.
echo [sftp]
echo host       = 116.204.65.72
echo port       = 22
echo username   = root
echo remote_dir = /opt/hospital_sync/data/pending
) > "%SCRIPT_DIR%config.ini"

echo [OK] config.ini 已生成
echo.
goto :eof
