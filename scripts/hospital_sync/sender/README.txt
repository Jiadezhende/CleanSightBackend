CleanSight 数据同步工具 — 部署说明
====================================

【文件清单】
  sync_agent.exe   主程序
  config.ini       配置文件（需填写）
  setup.bat        定时任务安装脚本
  README.txt       本文件


【第一步：填写配置文件】

用记事本打开 config.ini，修改以下内容：

  [sqlserver]
  server   = 10.168.1.170     ← 医院 SQL Server 的 IP，一般不用改
  database = master            ← 不用改
  username = sa                ← 数据库账号
  password = P@ssw0rd         ← 数据库密码，改成实际密码

  [linked_server]
  name     = NJXX              ← 链接服务器名称，一般不用改
  database = THIS4             ← 数据库名称，一般不用改

  [sftp]
  host     = 116.204.65.72    ← 不用改
  username = root              ← 不用改
  remote_dir = /opt/...       ← 不用改


【第二步：安装定时任务】

双击运行 setup.bat（需要管理员权限）。
安装后程序每天自动运行，无需手动操作。


【第三步：验证】

安装完成后，查看同目录下的 sync.log 文件：
- 看到 "done - synced: X, failed: 0" 表示成功
- 如有问题，将 sync.log 发给技术支持


【常见问题】

Q: 程序一直没有日志输出？
A: 删除同目录下的 sync.lock 文件，重新运行。

Q: 日志显示连接失败？
A: 确认 config.ini 中的 IP 和密码是否正确。

Q: 如何手动运行一次？
A: 双击 sync_agent.exe 即可。
