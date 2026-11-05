#!/usr/bin/env bash
# colima k8s 프로필의 Docker 빌드 캐시를 비우고 VM 디스크의 빈 블록을 호스트에 돌려준다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/cluster/docker-cache-gc.sh
#
# ⭐ 왜: BuildKit 빌드 캐시가 한 번 349GB 까지 쌓여 외장 SSD(/Volumes/TradingData)를 채웠다. VM 디스크 이미지는 SSD 에 있고,
#    VM 안에서 파일을 지워도 fstrim 을 하지 않으면 호스트의 이미지 파일은 줄지 않는다. 그래서 prune 뒤에 fstrim 까지 한다.
# ⭐ 언제: launchd(cluster/launchd/com.cafitac.docker-cache-gc.plist)가 매주 일요일 04:30.
# ⭐ 무엇을: `docker builder prune` 만 — 168 시간(7 일) 넘게 안 쓴 빌드 캐시. 이미지 · 볼륨 · 컨테이너는 건드리지 않는다
#    (`docker image prune` · `system prune` · `volume prune` 을 쓰지 않는다 — 쓰는 중인 데이터가 날아간다).
# ⭐ 알림: Discord. 웹훅 주소는 새로 만들지 않고 Alertmanager 가 쓰는 Secret(monitoring/alertmanager-discord, 키 webhook-url)을
#    kubectl 로 읽어 쓴다. k3s 가 colima 안에 있어서, colima 가 꺼져 있으면 Secret 도 못 읽는다 — 그때는 기록만 남긴다.
# ⭐ TCC: launchd 로 뜬 bash 는 외장 볼륨(/Volumes)에 쓸 수 없다(backup-offload.sh 와 같은 이유). /Volumes 는 df 로 읽기만 하고,
#    기록은 launchd 가 ~/Library/Logs 에 쓴다. 이 스크립트는 /Volumes 아래에 아무것도 쓰지 않는다.
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin

PROFILE=k8s
CONTEXT=colima-k8s
SSD=/Volumes/TradingData
KEEP=${KEEP:-168h}                       # 이보다 오래 안 쓴 캐시만 지운다
SECRET_NS=monitoring
SECRET_NAME=alertmanager-discord

log() { echo "$(date '+%F %T') $*"; }

# Discord 로 보낸다. 실패해도 스크립트를 죽이지 않는다(알림이 본 작업보다 중요하지 않다). 웹훅 주소는 출력하지 않는다.
notify() {
  local msg=$1 url
  url=$(kubectl -n "$SECRET_NS" get secret "$SECRET_NAME" -o jsonpath='{.data.webhook-url}' 2>/dev/null | base64 -d 2>/dev/null || true)
  if [ -z "$url" ]; then log "Discord 웹훅을 읽지 못했다(colima · k3s 가 꺼졌을 수 있다) — 알림 없이 기록만 남김"; return 0; fi
  curl -fsS -m 20 -H 'Content-Type: application/json' \
    -d "$(jq -n --arg c "$msg" '{content: $c}')" "$url" > /dev/null 2>&1 \
    || log "Discord 전송 실패 — 기록만 남김"
}

fail() { log "실패: $*"; notify ":warning: docker-cache-gc 실패 — $*"; exit 1; }

# 사용량(KB) — 읽기만 한다
used_kb() { df -k "$SSD" | awk 'NR==2 {print $3}'; }
# "1.5GB" · "512MB" · "0B" → 바이트(docker 는 10 진 단위)
to_bytes() {
  awk -v s="$1" 'BEGIN {
    n = s + 0; u = s; sub(/^[0-9.]+/, "", u)
    m = 1
    if (u == "kB" || u == "KB") m = 1e3; else if (u == "MB") m = 1e6; else if (u == "GB") m = 1e9; else if (u == "TB") m = 1e12
    printf "%.0f\n", n * m
  }'
}

mount | grep -q " on $SSD " || fail "외장 SSD 가 마운트되지 않았다 — 건너뜀"

# colima k8s 가 떠 있어야 한다. 꺼져 있으면 건드리지 않고 알린다.
colima status -p "$PROFILE" > /dev/null 2>&1 || fail "colima $PROFILE 이 실행 중이 아니다 — 건너뜀"

# 컨텍스트가 colima k8s 의 소켓을 가리키는지 읽기 전용으로 확인한다(다른 프로필을 비우는 사고 방지)
expect="unix://$HOME/.colima/$PROFILE/docker.sock"
actual=$(docker context inspect "$CONTEXT" --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)
[ "$actual" = "$expect" ] || fail "docker 컨텍스트 $CONTEXT 가 $expect 를 가리키지 않는다(실제: ${actual:-없음}) — 건너뜀"

before_kb=$(used_kb)
log "시작 — $SSD 사용 $((before_kb / 1024)) MB"

# 이미지 · 볼륨은 건드리지 않는다: builder prune 만, until 필터 필수
prune_out=$(docker --context "$CONTEXT" builder prune -f --filter "until=$KEEP" 2>&1) || { echo "$prune_out"; fail "builder prune 실패"; }
echo "$prune_out"
total=$(echo "$prune_out" | awk '/^Total:/ {print $2}' | tail -1)
reclaimed=$(to_bytes "${total:-0B}")

# VM 안의 빈 블록을 호스트 디스크 이미지에 돌려준다. stdin 을 닫는다(ssh 가 입력을 먹지 않게)
trim_out=$(colima ssh -p "$PROFILE" -- sudo fstrim -av < /dev/null 2>&1) || { echo "$trim_out"; fail "fstrim 실패(prune 은 끝났다)"; }
echo "$trim_out"

after_kb=$(used_kb)
freed_host=$(( (before_kb - after_kb) * 1024 ))
log "끝 — $SSD 사용 $((after_kb / 1024)) MB (전 $((before_kb / 1024)) MB)"
log "빌드 캐시 회수 ${reclaimed} 바이트(prune 보고) · 호스트 SSD 에서 줄어든 양 ${freed_host} 바이트"

notify ":broom: docker-cache-gc 완료 — 빌드 캐시 ${total:-0B} 회수, SSD 사용 $((before_kb / 1048576))GB → $((after_kb / 1048576))GB (until=$KEEP)"
