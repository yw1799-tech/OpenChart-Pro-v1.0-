# OpenChart Pro 部署到腾讯云轻量服务器

> 目标：让 OpenChart Pro 跑在 `http://43.165.185.243:8000`，开机自启 + 崩溃重启 + 每日备份。

---

## 🎯 一键部署（推荐）

**前提**：您本地 Windows 装有 OpenSSH（Win10+ 自带）。

### 步骤 1：在服务器**先改密码**（强烈建议，3 分钟）

```powershell
# 在 PowerShell 里
ssh ubuntu@43.165.185.243
# 第一次会问 yes/no，输 yes
# 输入临时密码: XU8V7;Ln+-KGC(e
```

登进去后立即：

```bash
# 改个强密码
passwd

# （可选但推荐）添加你本地 SSH 公钥
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# 把你本地 ~/.ssh/id_ed25519.pub 内容粘贴进去：
vim ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# 退出
exit
```

### 步骤 2：本地一键打包+上传+部署

在 **PowerShell** 里（注意是 PowerShell 不是 cmd）：

```powershell
cd "D:\OpenChart Pro"
powershell -ExecutionPolicy Bypass -File deploy\pack-and-upload.ps1
```

脚本会：
1. 打包项目（排除 `__pycache__/.git/logs/` 等垃圾）
2. scp 上传到服务器（会让你输入新密码）
3. ssh 进去解压 + 跑 `setup.sh`（apt 装依赖、Python venv、systemd 启动、防火墙、备份 cron 全做完）

**预期耗时：5-10 分钟**（主要是 pip install）

### 步骤 3：填 API Key（首次必做）

```bash
ssh ubuntu@43.165.185.243
sudo vim /opt/openchart/.env
```

至少要填：
```ini
DEEPSEEK_API_KEY=sk-xxx     # 你的 DeepSeek key
LLM_PROVIDER=deepseek
```

保存后：
```bash
sudo systemctl restart openchart
journalctl -u openchart -f    # 实时看日志，Ctrl+C 退出
```

### 步骤 4：浏览器验证

打开 http://43.165.185.243:8000 应能看到 K 线和数据。

---

## 📋 手动部署（如不想用 PowerShell 脚本）

### A. 在本地电脑上（任意一种）

```bash
# 用 WSL 或 git bash
cd "D:\OpenChart Pro"
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
    --exclude='logs/*' --exclude='*.png' --exclude='archive' \
    --exclude='debug_*.py' --exclude='diag_*.py' \
    -czf /tmp/openchart-pkg.tar.gz .

scp /tmp/openchart-pkg.tar.gz ubuntu@43.165.185.243:~/
```

### B. SSH 到服务器执行

```bash
ssh ubuntu@43.165.185.243

# 解压 + 一键安装
mkdir -p ~/openchart-pkg
tar -xzf ~/openchart-pkg.tar.gz -C ~/openchart-pkg
sudo bash ~/openchart-pkg/deploy/setup.sh
```

---

## 🔧 常用运维命令

```bash
# 服务状态 / 启停
systemctl status openchart
sudo systemctl restart openchart
sudo systemctl stop openchart

# 实时日志（Ctrl+C 退出）
journalctl -u openchart -f
journalctl -u openchart --since "10 min ago"
journalctl -u openchart -n 200      # 最近 200 行

# 看资源占用
htop
sudo systemctl status openchart  # 含内存使用

# 编辑配置后必须重启
sudo vim /opt/openchart/.env
sudo systemctl restart openchart

# 手动备份（自动备份在每天 03:30）
sudo /usr/local/bin/openchart-backup-db.sh
ls -lah /backup/

# 恢复某次备份
sudo systemctl stop openchart
sudo gunzip -c /backup/openchart_20260424_033000.db.gz > /opt/openchart/data/openchart.db
sudo chown ubuntu:ubuntu /opt/openchart/data/openchart.db
sudo systemctl start openchart

# 重新部署（同一台机器，更新代码）
# 本地重跑 pack-and-upload.ps1 即可，setup.sh 会自动覆盖代码
```

---

## 🌐 配域名 + HTTPS（可选）

如果有域名 `xxx.com`：

```bash
# 1) 域名 A 记录指向 43.165.185.243

# 2) 装 Caddy
sudo apt install -y caddy

# 3) 编辑配置
sudo cp /opt/openchart/deploy/Caddyfile /etc/caddy/Caddyfile
sudo vim /etc/caddy/Caddyfile   # 改 your-domain.com 为你的域名

# 4) 重启 Caddy（自动申请 Let's Encrypt 证书，1-2 分钟）
sudo systemctl restart caddy

# 5) 访问 https://your-domain.com 即可（自动 HTTPS）
```

---

## ⚠️ 服务器 2GB 内存监控

OpenChart Pro 实测占用 ~260 MB，2GB 总内存预留 1.7GB 系统。监控：

```bash
# 实时看内存（按 q 退出）
htop

# 单独看 openchart 进程占用
systemctl status openchart | grep Memory

# 如果接近 1.5GB 上限（systemd MemoryMax），考虑升级 4GB
# 升级方法：腾讯云控制台 → 重置 → 选 60 元/月 2C4G 套餐
```

---

## 🧯 故障排查

### 服务启不来
```bash
journalctl -u openchart -n 100   # 看完整错误
# 常见原因：
# - .env 缺 DEEPSEEK_API_KEY → 加上重启
# - 端口 8000 被占 → sudo lsof -i:8000
# - Python 依赖装失败 → 重跑 sudo bash ~/openchart-pkg/deploy/setup.sh
```

### 浏览器打不开 http://IP:8000
```bash
# 检查防火墙
sudo ufw status
# 应有: 8000/tcp ALLOW

# 检查腾讯云控制台 → 防火墙 → 入站规则
# 必须放行 8000 端口（或 443 如果用 Caddy）
```

### 数据库 WAL 涨太快
```bash
ls -lah /opt/openchart/data/
# 如果 .db-wal > 100MB，手动 checkpoint:
sudo -u ubuntu sqlite3 /opt/openchart/data/openchart.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

### 重置整个部署
```bash
sudo systemctl stop openchart
sudo rm -rf /opt/openchart
# 重跑本地 pack-and-upload.ps1
```

---

## 📦 部署文件清单（这个 deploy/ 目录里）

| 文件 | 作用 |
|---|---|
| `setup.sh` | 服务器端一键安装脚本（apt+Python+systemd+防火墙+cron） |
| `openchart.service` | systemd unit 文件（开机自启 + 崩溃重启 + 1.5GB 内存上限） |
| `backup-db.sh` | DB 热备份脚本（每天 03:30 自动跑） |
| `Caddyfile` | Caddy 反代 + 自动 HTTPS 模板（可选） |
| `pack-and-upload.ps1` | Windows 本地一键打包+上传+远程部署 |
| `README.md` | 这份文档 |
