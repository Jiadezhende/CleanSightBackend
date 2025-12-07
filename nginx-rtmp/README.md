# Nginx + RTMP 安装部署完整流程（Ubuntu 22.04）

## 一、环境说明

* 系统：Ubuntu 22.04
* 目标：

  * 支持 RTMP 推流/拉流
  * 支持 `/stat` 统计页面
  * 支持 `/health` 健康检查接口

---

## 二、彻底卸载系统自带 Nginx

```bash
sudo systemctl stop nginx

sudo apt purge -y nginx nginx-common nginx-core nginx-full
sudo apt autoremove -y

sudo rm -rf /etc/nginx
sudo rm -rf /var/log/nginx
sudo rm -rf /var/cache/nginx
sudo rm -f /usr/sbin/nginx
```

确认已删除：

```bash
which nginx
```

若无输出说明卸载成功。

---

## 三、安装编译环境

```bash
sudo apt update
sudo apt install -y build-essential libpcre3 libpcre3-dev \
libssl-dev zlib1g-dev git wget
```

---

## 四、下载源码

进入源码目录：

```bash
cd /usr/local/src
```

下载 Nginx：

```bash
sudo wget http://nginx.org/download/nginx-1.24.0.tar.gz
sudo tar -zxvf nginx-1.24.0.tar.gz
```

下载 RTMP 模块：

```bash
sudo git clone https://github.com/arut/nginx-rtmp-module.git
```

---

## 五、编译安装带 RTMP 的 Nginx

```bash
cd /usr/local/src/nginx-1.24.0

sudo ./configure \
--prefix=/usr/local/nginx \
--with-http_ssl_module \
--add-module=/usr/local/src/nginx-rtmp-module
```

开始编译：

```bash
sudo make
sudo make install
```

---

## 六、创建全局 nginx 命令

```bash
sudo ln -s /usr/local/nginx/sbin/nginx /usr/sbin/nginx
```

验证是否包含 RTMP：

```bash
nginx -V 2>&1 | grep rtmp
```

若看到 `--add-module=...nginx-rtmp-module` 表示成功。

---

## 七、准备运行目录权限

```bash
sudo mkdir -p /usr/local/nginx/logs
sudo chown -R root:root /usr/local/nginx
sudo chmod -R 755 /usr/local/nginx
```

（如果遇到权限问题，也可执行）

```bash
sudo chown -R $USER:$USER /usr/local/nginx
```

---

## 八、配置 nginx.conf

替换配置文件：

```bash
sudo cp nginx-rtmp/nginx.conf /usr/local/nginx/conf/nginx.conf
```

---

## 九、准备 stat 页面资源

复制 stat.xsl：

```bash
sudo cp /usr/local/src/nginx-rtmp-module/stat.xsl /usr/local/nginx/html/
```

---

## 十、测试并启动 Nginx

```bash
nginx -t
nginx
```

---

## 十一、验证服务是否正常

### 1. 查看端口监听

```bash
sudo ss -lntp | grep 1935
sudo ss -lntp | grep :80
```

### 2. 健康检查接口

```bash
curl http://127.0.0.1/health
```

返回：

```
ok
```

### 3. 状态页面

浏览器访问：

```
http://127.0.0.1/stat
```

---

## 十二、本地推流测试

安装 ffmpeg：

```bash
sudo apt install -y ffmpeg
```

使用本地视频文件推流：

```bash
ffmpeg -re -i test.mp4 -c copy -f flv rtmp://127.0.0.1:1935/live/test
```

查看是否出现在 `/stat` 页面。

---

## 十三、常用服务地址

```
RTMP 推流： rtmp://服务器IP:1935/live/流名
RTMP 拉流： rtmp://服务器IP:1935/live/流名
统计页面： http://服务器IP/stat
健康检查： http://服务器IP/health
```

---

## 十四、常见问题排查

### 1. 查看端口

```bash
ss -tulnp
```

### 2. 检查配置错误

```bash
nginx -t
```

### 3. 查看是否包含 RTMP

```bash
nginx -V 2>&1 | grep rtmp
```