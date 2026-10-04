#!/usr/bin/env bash
# judge-board 의 Secret 을 만든다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/apps/judge-board/secrets.sh
# ⭐ 값은 compose 시절의 judge-board.env(외장 SSD)에서 바로 Secret 으로 간다 — 화면 · 저장소에 남지 않는다
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
SRC=${SRC:-/Volumes/TradingData/judge-board/deploy/judge-board.env}
NS=judge-board
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
grep -E '^(BOARD_DB_PASSWORD|BOARD_INVITE|BOARD_OFFICIAL_LANES|BOARD_PRACTICE_LANES|RUNNER_TOKEN)=' "$SRC" \
  | kubectl -n "$NS" create secret generic board-env --from-env-file=/dev/stdin --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
echo "board-env ← $SRC"
