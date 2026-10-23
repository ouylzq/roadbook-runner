#!/bin/sh
# 看板数据备份：storage/ + reports/ + qa-agent.db → backups/<日期>/
#
# 2026-10-19 定的规矩：跑批数据是这套东西的唯一不可再生资产（场景/原子在 git 里，
# run 产物只在这里），丢了就没了。本脚本每天由定时任务跑一次；也可以随时手动跑。
#
# 实现：db 用 sqlite3 .backup 拿一致快照（服务运行中也能备）；storage/reports 用
# APFS clonefile（cp -c）秒级克隆、不占双份空间，失败退回普通拷贝。保留最近 14 份。
set -e
cd "$(dirname "$0")"

KEEP=14
DEST="backups/$(date +%Y%m%d)"

# 当天重跑 = 用新备份替换当天的，不嵌套
if [ -d "$DEST" ]; then rm -rf "$DEST"; fi
mkdir -p "$DEST"

if [ -f qa-agent.db ]; then
  sqlite3 qa-agent.db ".backup '$DEST/qa-agent.db'"
fi

for dir in storage reports; do
  [ -d "$dir" ] || continue
  cp -Rc "$dir" "$DEST/$dir" 2>/dev/null || cp -R "$dir" "$DEST/$dir"
done

# 过期清理：只留最近 KEEP 份（BSD head 不认负数行数，先数再裁）
count=$(ls -1d backups/2* 2>/dev/null | wc -l | tr -d ' ')
if [ "$count" -gt "$KEEP" ]; then
  ls -1d backups/2* | sort | head -n "$((count - KEEP))" | while read -r old; do
    rm -rf "$old"
  done
fi

runs=$(ls -1 storage 2>/dev/null | wc -l | tr -d ' ')
size=$(du -sh "$DEST" | cut -f1)
echo "已备份到 $DEST（storage $runs 个 run，$size）"
