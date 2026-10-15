#!/usr/bin/env bash
LOG="/home/lag/REPOS_HDD/PropMap-prod/memory_monitor.log"
echo "timestamp,vmsize_kb,vmsize_mb,vmrss_kb,vmrss_mb,threads,fd_count,wal_size_mb" > "$LOG"
for i in {1..12}; do
  sleep 300
  ts=$(date -Iseconds)
  pid=7
  if docker exec propmap-prod bash -c "kill -0 $pid 2>/dev/null"; then
    rss_kb=$(docker exec propmap-prod bash -c "grep VmRSS /proc/$pid/status | awk '{print \$2}'")
    rss_mb=$((rss_kb/1024))
    vmsize_kb=$(docker exec propmap-prod bash -c "grep VmSize /proc/$pid/status | awk '{print \$2}'")
    vmsize_mb=$((vmsize_kb/1024))
    threads=$(docker exec propmap-prod bash -c "grep Threads /proc/$pid/status | awk '{print \$2}'")
    fd_count=$(docker exec propmap-prod bash -c "ls /proc/$pid/fd | wc -l")
    wal_size=$(docker exec propmap-prod bash -c "stat -c%s /app/data/listings.sqlite-wal 2>/dev/null || echo 0")
    wal_mb=$((wal_size/1024/1024))
    echo "$ts,$vmsize_kb,$vmsize_mb,$rss_kb,$rss_mb,$threads,$fd_count,$wal_mb" >> "$LOG"
    echo "[$ts] RSS ${rss_mb}MB Threads $threads FDs $fd_count WAL ${wal_mb}MB"
  else
    echo "[$ts] PID $pid no encontrado"
  fi
done
