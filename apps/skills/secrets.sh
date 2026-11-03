#!/usr/bin/env bash
# owner 의 로컬 파일만 읽어 식별자가 공개 저장소에 남지 않게 한다.
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
ENV_FILE=${ENV_FILE:-/Volumes/TradingData/skills/access.env}
[ -f "$ENV_FILE" ] || { echo "$ENV_FILE 가 없다" >&2; exit 1; }
[ "$(stat -f %Lp "$ENV_FILE")" = 600 ] || { echo "$ENV_FILE 권한이 600 이 아니다" >&2; exit 1; }
for k in SKILLS_ACCESS_TEAM_DOMAIN SKILLS_ACCESS_AUD; do
  [ "$(grep -c "^$k=." "$ENV_FILE")" = 1 ] || { echo '필수 항목 개수가 다르다' >&2; exit 1; }
done
[ "$(awk 'END {print NR}' "$ENV_FILE")" = 2 ] || { echo '두 줄만 허용한다' >&2; exit 1; }
kubectl get ns skills >/dev/null 2>&1 || kubectl create ns skills >/dev/null
kubectl -n skills create secret generic skills-access --from-env-file=/dev/stdin --dry-run=client -o yaml < "$ENV_FILE" \
  | kubectl apply -f - >/dev/null
echo "$ENV_FILE (2 개)"
