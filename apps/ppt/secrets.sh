#!/usr/bin/env bash
# ppt 의 Secret 을 만든다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/apps/ppt/secrets.sh
# ppt-access: Cloudflare Access 앱 ppt 의 팀 도메인 · AUD 태그(web 이 JWT 를 검증할 때 쓴다).
#   값은 외장 SSD 의 /Volumes/TradingData/ppt/access.env(0600)에서 두 줄을 그대로 옮긴다:
#     PPT_ACCESS_TEAM_DOMAIN=<팀>.cloudflareaccess.com
#     PPT_ACCESS_AUD=<Access 앱 → 설정 → Application Audience (AUD) Tag>
#   비밀번호는 아니지만 공개 저장소에 내부 앱 식별자를 두지 않으려고 Secret 으로 둔다.
# (S4 에서 추가) ppt-claude: trading 과 같은 claude.env 파일을 worker 에 파일로 마운트한다 — 그때 이 스크립트에 넣는다.
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
ENV_FILE=${ENV_FILE:-/Volumes/TradingData/ppt/access.env}
NS=ppt

[ -f "$ENV_FILE" ] || { echo "$ENV_FILE 가 없다" >&2; exit 1; }
[ "$(stat -f %Lp "$ENV_FILE")" = 600 ] || { echo "$ENV_FILE 권한이 600 이 아니다" >&2; exit 1; }
for k in PPT_ACCESS_TEAM_DOMAIN PPT_ACCESS_AUD; do
  [ "$(grep -c "^$k=." "$ENV_FILE")" = 1 ] || { echo "$k 가 한 줄이 아니다" >&2; exit 1; }
done
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS" >/dev/null
grep -E '^PPT_ACCESS_(TEAM_DOMAIN|AUD)=' "$ENV_FILE" \
  | kubectl -n "$NS" create secret generic ppt-access --from-env-file=/dev/stdin --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
echo "ppt-access ← $ENV_FILE (2 개)"
