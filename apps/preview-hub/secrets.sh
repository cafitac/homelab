#!/usr/bin/env bash
# preview-hub 의 Secret 을 만든다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/apps/preview-hub/secrets.sh
# ⭐ 값은 colima preview-hub VM 의 /opt/phub(compose 시절)에서 바로 Secret 으로 간다 — 화면 · 저장소에 남지 않는다.
#    GitHub 토큰(PR ref · 코멘트)과 Access 검증 설정(팀 도메인 · AUD)만 옮긴다. 터널은 homelab 통합 터널이 대신한다
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
NS=preview-hub
VM="colima ssh -p preview-hub --"

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
$VM sudo cat /opt/phub/secrets/github_token \
  | kubectl -n "$NS" create secret generic hub-github --from-file=github_token=/dev/stdin \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null
$VM sudo grep -E '^PHUB_ACCESS_(TEAM_DOMAIN|AUD)=' /opt/phub/public.env \
  | kubectl -n "$NS" create secret generic hub-access --from-env-file=/dev/stdin \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "hub-github · hub-access 를 넣었다"
