#!/usr/bin/env bash
# 백업 PVC 의 덤프를 VM 밖(Mac Studio 내장 디스크 ~/Backups/k8s)으로 복사한다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/cluster/backup-offload.sh
#
# ⭐ 왜: 백업 CronJob 은 덤프를 백업 PVC 에 쓰는데, PVC 는 DB 와 같은 VM 디스크 이미지 안에 있다.
#    디스크 이미지가 깨지거나 `colima delete k8s` 를 하면 DB 와 백업이 함께 사라진다. 그래서 하루 한 번 밖으로 꺼낸다.
# ⭐ 언제: launchd(cluster/launchd/com.cafitac.backup-offload.plist)가 매일 05:00 — 백업 CronJob(04:00 · 04:30) 뒤.
# ⭐ 무엇을: 끝난 덤프(*.dump)만. 쓰는 중인 *.part · *.tmp 는 건너뛴다. 이미 같은 크기로 있으면 건너뛴다.
#    받는 중에는 .part 로 두고 크기가 맞을 때만 이름을 바꾼다. 밖의 사본은 KEEP_DAYS 일 보존(안쪽 14 · 7 일보다 길다).
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin

PROFILE=k8s
SSD=/Volumes/TradingData
KEEP_DAYS=${KEEP_DAYS:-30}
# 밖의 보관 폴더는 **내장 디스크**다. 두 가지 이유:
#  - VM 디스크 이미지는 외장 SSD 에 있다. 사본까지 SSD 에 두면 SSD 하나가 죽을 때 함께 사라진다.
#  - launchd 로 뜬 bash 는 외장 볼륨에 쓸 권한이 없다(TCC — Operation not permitted, 10/6 확인).
#    SSD 에 두려면 /bin/bash 에 전체 디스크 접근을 줘야 하는데, 그건 너무 넓다.
# 덤프는 작다(judge-board 수십 KB · interview-coach 10MB 안팎 × 30 일).
OUT=${OUT:-$HOME/Backups/k8s}
# 네임스페이스 → 밖의 보관 폴더
TARGETS=(
  "judge-board:$OUT/judge-board"
  "interview-coach:$OUT/interview-coach"
)

log() { echo "$(date '+%F %T') $*"; }

mount | grep -q " on $SSD " || { log "외장 SSD 가 마운트되지 않았다 — 건너뜀"; exit 1; }
# stdin 을 닫는다 — 안 닫으면 ssh 가 아래 while 의 목록을 먹어 첫 파일만 복사된다
vm() { colima -p "$PROFILE" ssh -- "$@" < /dev/null; }

failed=0
for t in "${TARGETS[@]}"; do
  ns=${t%%:*}
  dest=${t#*:}
  pv=$(kubectl -n "$ns" get pvc backups -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)
  dir=$( [ -n "$pv" ] && kubectl get pv "$pv" -o jsonpath='{.spec.hostPath.path}{.spec.local.path}' 2>/dev/null || true)
  if [ -z "$dir" ]; then log "$ns: 백업 PVC 를 찾지 못했다"; failed=1; continue; fi
  mkdir -p "$dest"

  copied=0
  # "크기 이름" 한 줄씩. 끝난 덤프만(*.dump). 목록을 먼저 다 받아 둔다 — 프로세스 치환으로 읽으면서 쓰면
  # 그 자식이 끝날 때의 신호가 SSD 의 파일 열기를 끊는다(Interrupted system call, 10/6 확인)
  list=$(vm sudo find "$dir" -maxdepth 1 -type f -name '*.dump' -printf '%s %f\n')
  while read -r size name; do
    [ -n "$name" ] || continue
    out=$dest/$name
    if [ -f "$out" ] && [ "$(stat -f %z "$out")" = "$size" ]; then continue; fi
    vm sudo cat "$dir/$name" > "$out.part"
    got=$(stat -f %z "$out.part")
    if [ "$got" != "$size" ]; then
      log "$ns: $name 크기가 다르다($got ≠ $size) — 버림"; rm -f "$out.part"; failed=1; continue
    fi
    mv "$out.part" "$out"
    copied=$((copied + 1))
  done <<< "$list"

  find "$dest" -name '*.dump' -mtime +"$KEEP_DAYS" -delete
  find "$dest" -name '*.part' -delete
  latest=$(ls "$dest"/*.dump 2>/dev/null | sort | tail -1 || true)   # 이름에 시각이 있다(복사한 시각 말고)
  log "$ns: 새로 $copied 개 · 보관 $(ls "$dest"/*.dump 2>/dev/null | wc -l | tr -d ' ') 개 · 최신 ${latest##*/}"
done
exit $failed
