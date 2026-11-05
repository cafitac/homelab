#!/usr/bin/env bash
# skills 러너의 Secret 을 만든다 — Mac Studio 에서 owner 가 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/apps/skills-runner/secrets.sh
# 값은 외장 SSD 의 skills-runner 전용 env 파일(0600)에서 바로 Secret 으로 간다. 화면 · 저장소 · 명령줄에 값이 남지 않는다.
# env 파일은 정확히 4 줄이다(이름만):
#   CLAUDE_CODE_OAUTH_TOKEN  SD-3 전용 setup-token                      → skills-runner/runner-claude
#   GH_TOKEN                 세분화된 PAT(저장소 두 개 · 90 일)           → skills-runner/runner-github
#   RUNNER_HMAC_KEY          소문자 16진수 64 자 이상                       → skills-runner/runner-hmac 와 skills/runner-hmac (키 이름 hmac-key, 두 곳이 같은 값)
#   ISSUED                   토큰 발급일 YYYY-MM-DD(비밀 아님)              → skills-runner/runner-token-issued ConfigMap (S10 만료 게이지의 입력)
# HMAC 키를 처음 만들 때(값이 출력되지 않는다):
#   printf 'RUNNER_HMAC_KEY=%s\n' "$(openssl rand -hex 32)" >> "$ENV_FILE"
# ANTHROPIC_API_KEY 는 넣지 않는다(4 줄 검사에서 거부된다). 바꾼 뒤에는 웹 파드를 재시작하고 러너 파드를 다시 띄워야 반영된다(Secret 은 파드가 뜰 때 읽는다; 폐기 순서는 REVOKE.md).
set -euo pipefail
export PATH=$PATH:/opt/homebrew/bin
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
ENV_FILE=${ENV_FILE:-/Volumes/TradingData/skills-runner/env}
RUNNER_NS=skills-runner
WEB_NS=skills

[ -f "$ENV_FILE" ] || { echo "$ENV_FILE 가 없다" >&2; exit 1; }
mode=$(stat -f %Lp "$ENV_FILE" 2>/dev/null || stat -c %a "$ENV_FILE")
[ "$mode" = 600 ] || { echo "$ENV_FILE 권한이 600 이 아니다" >&2; exit 1; }
[ "$(awk 'END {print NR}' "$ENV_FILE")" = 4 ] || { echo '4 줄만 허용한다' >&2; exit 1; }
for k in CLAUDE_CODE_OAUTH_TOKEN GH_TOKEN; do
  [ "$(grep -Ec "^$k=[^[:space:]]+\$" "$ENV_FILE")" = 1 ] || { echo "$k 가 정확히 한 줄이 아니다" >&2; exit 1; }
done
[ "$(grep -Ec '^RUNNER_HMAC_KEY=[0-9a-f]{64,}$' "$ENV_FILE")" = 1 ] || { echo 'RUNNER_HMAC_KEY 가 소문자 16진수 64 자 이상 한 줄이 아니다' >&2; exit 1; }
[ "$(grep -Ec '^ISSUED=[0-9]{4}-[0-9]{2}-[0-9]{2}$' "$ENV_FILE")" = 1 ] || { echo 'ISSUED 가 YYYY-MM-DD 한 줄이 아니다' >&2; exit 1; }
# 이 namespace 들은 Argo 가 만든다(apps/skills-runner · apps/skills). 없으면 머지 · 동기화가 끝나지 않은 것이다.
for ns in "$RUNNER_NS" "$WEB_NS"; do
  kubectl get ns "$ns" >/dev/null 2>&1 || { echo "namespace $ns 가 없다 — Argo 동기화(Synced/Healthy) 뒤에 실행한다" >&2; exit 1; }
done

apply_secret() { # <namespace> <secret 이름> <env 파일에서 고를 줄의 정규식> [키 이름 바꾸기 sed 식]
  grep -E "$3" "$ENV_FILE" | sed -e "${4:-s/^//}" \
    | kubectl -n "$1" create secret generic "$2" --from-env-file=/dev/stdin --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
}
apply_secret "$RUNNER_NS" runner-claude '^CLAUDE_CODE_OAUTH_TOKEN='
apply_secret "$RUNNER_NS" runner-github '^GH_TOKEN='
apply_secret "$RUNNER_NS" runner-hmac '^RUNNER_HMAC_KEY=' 's/^RUNNER_HMAC_KEY=/hmac-key=/'
apply_secret "$WEB_NS" runner-hmac '^RUNNER_HMAC_KEY=' 's/^RUNNER_HMAC_KEY=/hmac-key=/'
grep '^ISSUED=' "$ENV_FILE" \
  | kubectl -n "$RUNNER_NS" create configmap runner-token-issued --from-env-file=/dev/stdin --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
echo "runner-claude · runner-github · runner-hmac ← $ENV_FILE ($RUNNER_NS), runner-hmac ($WEB_NS), runner-token-issued (ConfigMap)"
