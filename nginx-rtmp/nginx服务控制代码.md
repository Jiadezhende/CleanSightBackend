# Nginx 服务控制代码
### 启动 Nginx
nginx

### 停止 Nginx
nginx -s stop

### 强制停止（更快）
nginx -s quit

### 重启 Nginx
nginx -s stop
nginx

### 平滑重载配置（推荐）
nginx -t
nginx -s reload

### 查看 Nginx 进程
ps -ef | grep nginx

### 查看端口监听
ss -tulnp | grep nginx