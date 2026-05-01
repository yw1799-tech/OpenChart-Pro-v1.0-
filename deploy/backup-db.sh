#!/usr/bin/env bash
# OpenChart Pro DB 自动备份（每天凌晨 03:30 由 cron 触发）
# 用 sqlite3 .backup 命令做热备份（即使数据库正在被写入也能安全拷贝）
set -euo pipefail

DB_FILE="/opt/openchart/data/openchart.db"
BACKUP_DIR="/backup"
TS=$(date +%Y%m%d_%H%M%S)
DEST="$BACKUP_DIR/openchart_${TS}.db"
KEEP_DAYS=14

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_FILE" ]; then
    echo "[$(date)] DB 文件不存在: $DB_FILE"
    exit 0
fi

# sqlite3 .backup 是热备份（在线，安全）
sqlite3 "$DB_FILE" ".backup '$DEST'"
gzip -9 "$DEST"

# 清理 14 天以前的旧备份
find "$BACKUP_DIR" -name 'openchart_*.db.gz' -mtime +${KEEP_DAYS} -delete 2>/dev/null || true

SIZE=$(du -h "${DEST}.gz" | cut -f1)
COUNT=$(find "$BACKUP_DIR" -name 'openchart_*.db.gz' | wc -l)
echo "[$(date)] ✓ 备份成功: ${DEST}.gz ($SIZE)，保留 $COUNT 份"
