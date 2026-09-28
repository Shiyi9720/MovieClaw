#!/usr/bin/env bash
# =============================================================================
# 演示站的黄金快照与每日还原（docs/design/demo-site.md §6）
#
#   ./reset.sh snapshot   建站完成后执行一次：停服务 → 把 data/ 打成 golden-data.tar.gz
#   ./reset.sh restore    每天由 cron 执行：停服务 → 用快照整体替换 data/ → 启动
#
# 演示模式下访客能留下的只有播放进度、收藏 / 已看、登录设备这些，每天整体还原
# 一次，第二天的访客看到的永远是同一个干净的演示站。
#
# 用 root 执行（data/ 里的文件属于容器内的 root）。crontab 示例：
#   17 4 * * * cd /srv/movieclaw-demo && ./reset.sh restore >> reset.log 2>&1
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

SNAPSHOT="golden-data.tar.gz"
stamp() { date '+%F %T'; }

case "${1:-}" in
    snapshot)
        [[ -d data ]] || { echo "当前目录下没有 data/，先完成建站" >&2; exit 1; }
        docker compose stop movieclaw
        tar -czf "$SNAPSHOT.tmp" data
        mv "$SNAPSHOT.tmp" "$SNAPSHOT"
        docker compose start movieclaw
        echo "$(stamp) 黄金快照已更新：$SNAPSHOT（$(du -h "$SNAPSHOT" | cut -f1)）"
        ;;
    restore)
        [[ -f "$SNAPSHOT" ]] || { echo "找不到 $SNAPSHOT，先执行 ./reset.sh snapshot" >&2; exit 1; }
        # 先解到旁边再换，解压失败时现有 data/ 原封不动
        rm -rf data.restoring
        mkdir data.restoring
        tar -xzf "$SNAPSHOT" -C data.restoring
        docker compose stop movieclaw
        rm -rf data.previous
        mv data data.previous
        mv data.restoring/data data
        rmdir data.restoring
        docker compose start movieclaw
        rm -rf data.previous
        echo "$(stamp) 已还原到黄金快照"
        ;;
    *)
        echo "用法：$0 snapshot|restore" >&2
        exit 2
        ;;
esac
