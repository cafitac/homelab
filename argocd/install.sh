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
helm upgrade --install argocd argo/argo-cd --version "$ARGOCD_CHART_VERSION" \
  -n argocd -f values.yaml --wait --timeout 10m
kubectl apply -k .
echo "Argo CD 준비됨 — https://argocd.cafitac.com (admin 비밀번호: kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"
