#!/usr/bin/env bash
# OpenChart Pro 一键部署脚本（Ubuntu 24.04）
# 用法：在服务器 ~/openchart-pkg 目录解压后 sudo bash deploy/setup.sh
set -euo pipefail

APP_USER="ubuntu"
APP_DIR="/opt/openchart"
SERVICE_NAME="openchart"
PY_BIN="python3"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  OpenChart Pro 自动部署 ($(date))"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── 1. 系统包 ───
echo "[1/8] 装系统依赖..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ufw fail2ban tzdata sqlite3 build-essential

# ─── 2. 时区 ───
echo "[2/8] 设置时区为 Asia/Shanghai..."
timedatectl set-timezone Asia/Shanghai || true

# ─── 3. 应用目录 ───
echo "[3/8] 准备 $APP_DIR..."
mkdir -p "$APP_DIR" "$APP_DIR/data" "$APP_DIR/logs" "/backup"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "/backup"

# ─── 4. 拷贝代码（假设代码包已解压到 ~/openchart-pkg） ───
PKG_DIR="${PKG_DIR:-/home/$APP_USER/openchart-pkg}"
if [ ! -f "$PKG_DIR/run.py" ]; then
    echo "❌ 找不到 $PKG_DIR/run.py，请先把代码包解压到该目录"
    exit 1
fi
echo "[4/8] 复制代码 $PKG_DIR → $APP_DIR..."
# 关键：必须排除 .env，否则远程真实 API Key 会被本地空 .env 覆盖
# 2026-04-29 事故：DEEPSEEK_API_KEY 被空字符串覆盖导致全部 LLM 调用失败 4h
rsync -a --delete \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.git' --exclude='node_modules' \
    --exclude='data' --exclude='data/**' \
    --exclude='logs' --exclude='logs/**' \
    --exclude='.env' --exclude='.env.local' --exclude='.env.production' \
    "$PKG_DIR/" "$APP_DIR/"
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ─── 5. Python 虚拟环境 + 依赖 ───
echo "[5/8] 创建 venv + 装 Python 依赖（5-10 分钟）..."
sudo -u "$APP_USER" $PY_BIN -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip wheel -q
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "  ✓ 依赖安装完成"

# ─── 6. .env 配置（如果不存在则从模板生成） ───
if [ ! -f "$APP_DIR/.env" ]; then
    echo "[6/8] 创建 .env（请部署后编辑填 API Key）..."
    cp "$APP_DIR/.env.example" "$APP_DIR/.env" 2>/dev/null || true
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
else
    echo "[6/8] .env 已存在，跳过"
fi

# ─── 7. systemd 服务 ───
echo "[7/8] 安装 systemd 服务..."
cp "$APP_DIR/deploy/openchart.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "  ✓ $SERVICE_NAME 已启动"
else
    echo "  ❌ $SERVICE_NAME 启动失败，运行 journalctl -u $SERVICE_NAME -n 50 查看"
fi

# ─── 8. 防火墙 + fail2ban ───
echo "[8/8] 配置防火墙 + fail2ban..."
ufw --force enable >/dev/null 2>&1 || true
ufw allow 22/tcp comment 'SSH' >/dev/null 2>&1
ufw allow 80/tcp comment 'HTTP' >/dev/null 2>&1
ufw allow 443/tcp comment 'HTTPS' >/dev/null 2>&1
ufw allow 8000/tcp comment 'OpenChart' >/dev/null 2>&1
systemctl enable fail2ban >/dev/null 2>&1
systemctl restart fail2ban >/dev/null 2>&1

# ─── 备份脚本 + cron ───
cp "$APP_DIR/deploy/backup-db.sh" /usr/local/bin/openchart-backup-db.sh
chmod +x /usr/local/bin/openchart-backup-db.sh
# crontab：每天 03:30 备份，保留 14 天
(sudo -u "$APP_USER" crontab -l 2>/dev/null | grep -v openchart-backup-db || true; \
 echo "30 3 * * * /usr/local/bin/openchart-backup-db.sh >> /var/log/openchart-backup.log 2>&1") | \
    sudo -u "$APP_USER" crontab - 2>/dev/null || \
    (echo "30 3 * * * /usr/local/bin/openchart-backup-db.sh >> /var/log/openchart-backup.log 2>&1" > /etc/cron.d/openchart-backup && chmod 644 /etc/cron.d/openchart-backup)

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ 部署完成"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
PUBLIC_IP=$(curl -s --max-time 3 ifconfig.me || echo "<your-ip>")
echo
echo "  访问:           http://${PUBLIC_IP}:8000"
echo "  服务状态:       systemctl status $SERVICE_NAME"
echo "  实时日志:       journalctl -u $SERVICE_NAME -f"
echo "  重启服务:       sudo systemctl restart $SERVICE_NAME"
echo "  编辑 .env:      vim $APP_DIR/.env  &&  sudo systemctl restart $SERVICE_NAME"
echo
echo "  ⚠️ 首次部署请编辑 .env 填入 DEEPSEEK_API_KEY，再 restart 服务"
echo "  ⚠️ 如启用 WhatsApp 通知，前端 'WhatsApp 配置' 弹窗保存后会自动写入"
