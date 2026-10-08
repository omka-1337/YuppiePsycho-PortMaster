#!/bin/bash
# Memory watchdog for the game process.
#
#   memwatch.sh PID LOGFILE
#
# Logs system and game memory twice a second and kills the game before the
# device runs out of memory: on a 1 GB handheld an exhausted system thrashes
# and freezes instead of letting the kernel's OOM killer act. GPU buffers
# (Panfrost) are shmem pages, so they show up in the "shmem" column.
#
# Environment:
#   YP_MEMWATCH_MIN_MB   kill when available RAM and free swap are both below
#                        this (default 64)
#   YP_MEMWATCH_PERIOD   sampling period in seconds (default 0.5)

pid="$1"
log="$2"
min_mb="${YP_MEMWATCH_MIN_MB:-64}"
period="${YP_MEMWATCH_PERIOD:-0.5}"

# Let the kernel pick the game, never us, if it has to kill something.
echo 1000 > "/proc/$pid/oom_score_adj" 2>/dev/null
echo -1000 > "/proc/$$/oom_score_adj" 2>/dev/null

sleep 0.01 2>/dev/null || period=1

start=$(date +%s)
low=0
peak_rss=0
peak_shmem=0
echo "# t_s avail_mb swapfree_mb shmem_mb game_rss_mb game_swap_mb" > "$log"
while kill -0 "$pid" 2>/dev/null; do
  read -r avail swapfree shmem < <(awk '
    /^MemAvailable:/ { a = $2 } /^SwapFree:/ { s = $2 } /^Shmem:/ { h = $2 }
    END { printf "%d %d %d\n", a / 1024, s / 1024, h / 1024 }' /proc/meminfo)
  read -r rss vswap < <(awk '
    /^VmRSS:/ { r = $2 } /^VmSwap:/ { w = $2 }
    END { printf "%d %d\n", r / 1024, w / 1024 }' "/proc/$pid/status" 2>/dev/null)
  rss="${rss:-0}"
  [ "$rss" -gt "$peak_rss" ] && peak_rss=$rss
  [ "$shmem" -gt "$peak_shmem" ] && peak_shmem=$shmem
  echo "$(( $(date +%s) - start )) $avail $swapfree $shmem $rss ${vswap:-0}" >> "$log"
  if [ "$avail" -lt "$min_mb" ] && [ "$swapfree" -lt "$min_mb" ]; then
    low=$((low + 1))
  else
    low=0
  fi
  if [ $low -ge 2 ]; then
    echo "# memwatch: out of memory (available ${avail} MB, free swap ${swapfree} MB," \
         "game RSS ${rss} MB, shmem/GPU ${shmem} MB) - game killed" | tee -a "$log"
    kill -9 "$pid" 2>/dev/null
    break
  fi
  sleep "$period"
done
echo "# peak game RSS ${peak_rss} MB, peak shmem/GPU ${peak_shmem} MB" >> "$log"
