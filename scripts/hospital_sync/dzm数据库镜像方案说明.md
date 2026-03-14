# hospital_sync — 医院视图镜像同步

将医院 DZM 机（Windows 64位）SQL Server 中的所有视图，定期导出并同步到业务服务器（CentOS）。

---

## 整体流程

```text
DZM 机（Windows）                         业务服务器（CentOS）
─────────────────                         ─────────────────────
SQL Server 视图
  ↓ pyodbc 流式读取（5000行/批）
  ↓ 导出 CSV → gzip 压缩
  ↓ SFTP 断点续传（私钥认证）
                          ──────────────→ data/pending/<view>.csv.gz
                                                ↓ receiver 轮询（30s）
                                                ↓ 解压 → data/csv/<view>.csv
                                                ↓ 归档 → data/done/（保留7天）
                                                ↓ POST 通知业务服务（待接入）
                                          业务服务 → TRUNCATE + COPY → PostgreSQL
```

---

## 目录结构

```text
hospital_sync/
├── sender/                    # 运行于 DZM 机（Windows）
│   ├── sync_agent.py          # 主脚本：导出→压缩→上传
│   ├── config.ini.example     # 配置模板（复制为 config.ini 后填写）
│   ├── hospital_sync_key      # SFTP 私钥（不进 git）
│   ├── hospital_sync_key.pub  # SFTP 公钥（部署到服务器 authorized_keys）
│   └── setup.bat              # 一键部署：注册 Windows 任务计划
│
├── receiver/                  # 运行于业务服务器（CentOS）
│   ├── data_receiver.py       # 主脚本：解压→归档→通知
│   └── data-receiver.service  # systemd 服务配置
│
└── sql/                       # 测试用 SQL（对接真实 DZM 机时不需要）
    ├── 01_init_schema.sql     # 建库建表建视图
    ├── 02_init_data.sql       # 插入测试数据
    └── 00_drop_schema.sql     # 清理（重置测试环境用）
```

---

## 当前进度

| 步骤 | 状态 | 备注 |
| --- | --- | --- |
| sender 脚本开发 | ✅ 完成 | 支持快照隔离、断点续传、失败重试 |
| receiver 脚本开发 | ✅ 完成 | 支持稳定性检测、归档、定期清理 |
| receiver systemd 部署 | ✅ 完成 | 服务器 `/opt/hospital_sync/`，已 enable |
| SFTP 密钥对生成 | ✅ 完成 | 公钥待加入服务器 authorized_keys |
| sender 本地测试（Docker SQL Server） | ✅ 完成 | 本地 Docker 环境验证通过 |
| sender → receiver 端到端联调 | ✅ 完成 | CSV 成功传输并解压 |
| 业务服务导入接口对接 | ⬜ 待完成 | 接口路径待确认，填写 `IMPORT_NOTIFY_URL` |
| DZM 机真实 SQL Server 对接 | ⬜ 待完成 | 需确认驱动版本与视图权限 |
| pyinstaller 打包（开发机执行） | ⬜ 待完成 | 产出 sync_agent.exe |
| 传包到 DZM + setup.bat 部署 | ⬜ 待完成 | DZM 无需安装 Python |

---

## 快速部署参考

### 业务服务器（CentOS）

```bash
# 1. 创建目录
mkdir -p /opt/hospital_sync/data/{pending,csv,done}

# 2. 上传脚本与 service 文件
scp receiver/data_receiver.py root@<server>:/opt/hospital_sync/
scp receiver/data-receiver.service root@<server>:/etc/systemd/system/

# 3. 启动服务
systemctl daemon-reload && systemctl enable --now data-receiver

# 4. 将公钥加入 authorized_keys
cat sender/hospital_sync_key.pub | ssh root@<server> "cat >> /root/.ssh/authorized_keys"
```

### DZM 机部署流程（开发机 → DZM，无需 Python）

#### 第一步：在开发机打包

```powershell
cd scripts/hospital_sync/sender
pip install pyinstaller
pyinstaller --onefile sync_agent.py
# 产出：dist/sync_agent.exe
```

#### 第二步：将以下三个文件拷贝到 DZM 机同一目录

```text
sync_agent.exe        ← pyinstaller 打包产物
config.ini            ← 填写 DZM 真实 SQL Server + SFTP 配置
hospital_sync_key     ← SFTP 私钥
```

#### 第三步：在 DZM 机以管理员身份运行 setup.bat

右键 `setup.bat` → 以管理员身份运行

setup.bat 会自动：

- 将三个文件复制到 `C:\hospital_sync\`
- 收紧私钥文件权限
- 注册 Windows 任务计划（每天 06:00 / 12:00 / 18:00）
- 执行首次同步测试
