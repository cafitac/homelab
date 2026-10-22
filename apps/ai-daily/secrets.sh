#!/usr/bin/env bash
# ai-daily 의 Secret 을 만든다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/apps/ai-daily/secrets.sh
# 값은 외장 SSD 의 ai-daily 전용 env 파일(0600)에서 CLAUDE_CODE_OAUTH_TOKEN 한 줄만 골라 바로 Secret 으로 간다.
# 화면 · 저장소 · 명령줄에 값이 남지 않는다. ANTHROPIC_API_KEY 는 넣지 않는다(있으면 ai-daily 가 실행을 거부한다).
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
ENV_FILE=${ENV_FILE:-/Volumes/TradingData/ai-daily/env}
NS=ai-daily

[ -f "$ENV_FILE" ] || { echo "$ENV_FILE 가 없다" >&2; exit 1; }
[ "$(stat -f %Lp "$ENV_FILE")" = 600 ] || { echo "$ENV_FILE 권한이 600 이 아니다" >&2; exit 1; }
[ "$(grep -c '^CLAUDE_CODE_OAUTH_TOKEN=.' "$ENV_FILE")" = 1 ] || { echo "CLAUDE_CODE_OAUTH_TOKEN 이 한 줄이 아니다" >&2; exit 1; }
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS" >/dev/null
grep '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE" \
  | kubectl -n "$NS" create secret generic ai-daily-env --from-env-file=/dev/stdin --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
echo "ai-daily-env ← $ENV_FILE (CLAUDE_CODE_OAUTH_TOKEN 1 개)"
