#!/usr/bin/env bash
# Argo CD 를 올리고 루트 Application(App of Apps)을 건다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/argocd/install.sh
# ⭐ 이후로는 이 저장소의 argocd/apps/ 가 클러스터의 원본이다. 바꾸고 push 하면 Argo CD 가 맞춘다.
# ⭐ Argo CD 자신은 이 스크립트(helm)로 관리한다 — 스스로를 지우는 실수를 피한다.
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
cd "$(dirname "$0")"

ARGOCD_CHART_VERSION=10.9.6

kubectl apply -f namespace.yaml
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null
# ⭐ SSO — Cloudflare Access 를 OIDC 공급자로 쓴다. Client ID 는 Secret argocd-oidc 에만 있다(공개 저장소에 두지 않는다).
#    Secret 이 없으면 SSO 없이 올린다 — argocd/set-oidc-secret.sh 로 넣고 다시 실행한다
EXTRA=()
if kubectl -n argocd get secret argocd-oidc >/dev/null 2>&1; then
  CLIENT_ID=$(kubectl -n argocd get secret argocd-oidc -o jsonpath='{.data.clientID}' | base64 -d)
  OIDC=$(mktemp); trap 'rm -f "$OIDC"' EXIT
  cat > "$OIDC" <<YAML
configs:
  cm:
    oidc.config: |
      name: Cloudflare Access
      issuer: https://cafitac.cloudflareaccess.com/cdn-cgi/access/sso/oidc/$CLIENT_ID
      clientID: \$argocd-oidc:clientID
      clientSecret: \$argocd-oidc:clientSecret
      requestedScopes: [openid, email, profile]
YAML
  EXTRA=(-f "$OIDC")
fi
helm upgrade --install argocd argo/argo-cd --version "$ARGOCD_CHART_VERSION" \
  -n argocd -f values.yaml "${EXTRA[@]}" --wait --timeout 10m
kubectl apply -k .
# 로컬 admin 을 껐으므로 처음 만들어진 비밀번호도 남기지 않는다
kubectl -n argocd delete secret argocd-initial-admin-secret --ignore-not-found >/dev/null
echo "Argo CD 준비됨 — https://argocd.cafitac.com (Cloudflare Access SSO)"
