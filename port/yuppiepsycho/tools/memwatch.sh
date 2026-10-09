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
# The last columns help with stutter: CPU use of the whole game and of its
# main thread, which runs the game loop (100 = one core busy; a main thread
# near 100 means the game cannot keep up), the clock of CPU 0 and the SoC
# temperature (throttling).
#
# Free swap only counts as headroom while GPU buffers are a minor part of
# memory: they are pinned and cannot be swapped out, and zram itself lives in
# RAM. Measured on an RG40XX H: the game pinned 700 MB of textures with 700 MB
# of zram still free, and the device froze.
#
# Environment:
#   YP_MEMWATCH_MIN_MB   kill when available RAM stays below this (default 64)
#   YP_MEMWATCH_PERIOD   sampling period in seconds (default 0.5)
#   YP_MEMWATCH_MEMINFO  meminfo file to read (tests)

pid="$1"
log="$2"
min_mb="${YP_MEMWATCH_MIN_MB:-64}"
period="${YP_MEMWATCH_PERIOD:-0.5}"
meminfo="${YP_MEMWATCH_MEMINFO:-/proc/meminfo}"

# Let the kernel pick the game, never us, if it has to kill something.
echo 1000 > "/proc/$pid/oom_score_adj" 2>/dev/null
echo -1000 > "/proc/$$/oom_score_adj" 2>/dev/null

sleep 0.01 2>/dev/null || period=1

total_mb=$(awk '/^MemTotal:/ { print int($2 / 1024) }' "$meminfo")
gpu_heavy_mb=$(( ${total_mb:-1024} / 2 ))
start=$(date +%s)
low=0
peak_rss=0
peak_shmem=0
hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
freq_file=/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
temp_file=/sys/class/thermal/thermal_zone0/temp
prev=""
echo "# t_s avail_mb swapfree_mb shmem_mb game_rss_mb game_swap_mb game_cpu_pct main_cpu_pct cpu_mhz temp_c" > "$log"
while kill -0 "$pid" 2>/dev/null; do
  read -r avail swapfree shmem < <(awk '
    /^MemAvailable:/ { a = $2 } /^SwapFree:/ { s = $2 } /^Shmem:/ { h = $2 }
    END { printf "%d %d %d\n", a / 1024, s / 1024, h / 1024 }' "$meminfo")
  read -r rss vswap < <(awk '
    /^VmRSS:/ { r = $2 } /^VmSwap:/ { w = $2 }
    END { printf "%d %d\n", r / 1024, w / 1024 }' "/proc/$pid/status" 2>/dev/null)
  rss="${rss:-0}"
  # Uptime, then utime + stime (fields 14 and 15, counted after the "(comm)"
  # field) of the process and of its main thread.
  now=$(awk 'FILENAME == "/proc/uptime" { printf "%s", $1; next }
    { sub(/^.*\) /, ""); printf " %d", $12 + $13 }' /proc/uptime "/proc/$pid/stat" \
    "/proc/$pid/task/$pid/stat" 2>/dev/null)
  read -r cpu main_cpu < <(awk -v now="$now" -v prev="$prev" -v hz="$hz" 'BEGIN {
    n = split(now, a, " "); m = split(prev, b, " ")
    if (n < 3 || m < 3 || a[1] <= b[1]) { print "- -"; exit }
    d = a[1] - b[1]
    printf "%d %d\n", (a[2] - b[2]) * 100 / hz / d, (a[3] - b[3]) * 100 / hz / d }')
  prev="$now"
  mhz=$(awk '{ printf "%d", $1 / 1000 }' "$freq_file" 2>/dev/null)
  temp=$(awk '{ printf "%d", $1 / 1000 }' "$temp_file" 2>/dev/null)
  [ "$rss" -gt "$peak_rss" ] && peak_rss=$rss
  [ "$shmem" -gt "$peak_shmem" ] && peak_shmem=$shmem
  echo "$(( $(date +%s) - start )) $avail $swapfree $shmem $rss ${vswap:-0} $cpu $main_cpu ${mhz:--} ${temp:--}" >> "$log"
  if [ "$avail" -lt "$min_mb" ] && { [ "$swapfree" -lt "$min_mb" ] || [ "$shmem" -gt "$gpu_heavy_mb" ]; }; then
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
