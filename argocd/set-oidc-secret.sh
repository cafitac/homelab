#!/usr/bin/env bash
# Cloudflare Access(SaaS · OIDC 앱 argocd)의 Client ID · Client secret 을 Secret 으로 넣는다 — Mac Studio 에서 사람이 실행한다.
#   ssh -t trading-macstudio 'bash ~/Project/homelab/argocd/set-oidc-secret.sh'
# ⭐ 값은 화면 · 저장소 · 대화에 남지 않는다. secret 은 입력할 때 보이지 않는다.
# 값 위치: Cloudflare One → Access controls → Applications → argocd → Client ID / Client secret
#          secret 이 안 보이면 「Reset secret」으로 새로 만든다(새 값만 보인다)
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}

read -r -p "Client ID: " CLIENT_ID
read -r -s -p "Client secret (입력이 보이지 않습니다): " CLIENT_SECRET; echo
[ -n "$CLIENT_ID" ] && [ -n "$CLIENT_SECRET" ] || { echo "둘 다 필요하다" >&2; exit 1; }

# Client ID 가 맞는지 Cloudflare 의 OIDC 설정 주소로 확인한다
code=$(curl -s -o /dev/null -w '%{http_code}' "https://cafitac.cloudflareaccess.com/cdn-cgi/access/sso/oidc/$CLIENT_ID/.well-known/openid-configuration")
[ "$code" = 200 ] || { echo "Client ID 가 맞지 않다(OIDC 설정 주소 $code)" >&2; exit 1; }

kubectl -n argocd create secret generic argocd-oidc \
  --from-literal=clientID="$CLIENT_ID" --from-literal=clientSecret="$CLIENT_SECRET" \
  --dry-run=client -o yaml \
  | kubectl label --local -f - app.kubernetes.io/part-of=argocd -o yaml \
  | kubectl apply -f - >/dev/null
unset CLIENT_SECRET
echo "Secret argocd/argocd-oidc 를 넣었다. 다음: bash ~/Project/homelab/argocd/install.sh"
