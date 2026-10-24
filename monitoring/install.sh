#!/usr/bin/env bash
# 모니터링을 올린다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/monitoring/install.sh
# ⚠️ 2026-10-05 부터 Argo CD(argocd/apps/monitoring.yaml)가 관리한다. 이 스크립트는 Argo CD 없이 처음 세울 때만 쓴다.
#    버전을 바꿀 때는 argocd/apps/monitoring.yaml 의 targetRevision 도 같이 바꾼다.
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
cd "$(dirname "$0")"

KPS_VERSION=91.9.0
BLACKBOX_VERSION=11.19.1
LOKI_VERSION=7.5.0
ALLOY_VERSION=1.14.3

kubectl apply -f namespace.yaml
# Grafana 관리자 비밀번호 — 처음 한 번 무작위로 만든다. 확인: kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d
kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1 || \
  kubectl -n monitoring create secret generic grafana-admin \
    --from-literal=admin-user=admin --from-literal=admin-password="$(openssl rand -base64 24)"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null
helm upgrade --install kps prometheus-community/kube-prometheus-stack --version "$KPS_VERSION" \
  -n monitoring -f kube-prometheus-stack.values.yaml --wait --timeout 10m
helm upgrade --install blackbox prometheus-community/prometheus-blackbox-exporter --version "$BLACKBOX_VERSION" \
  -n monitoring -f blackbox.values.yaml --wait
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update grafana >/dev/null
helm upgrade --install loki grafana/loki --version "$LOKI_VERSION" -n monitoring -f loki.values.yaml --wait
helm upgrade --install alloy grafana/alloy --version "$ALLOY_VERSION" -n monitoring -f alloy.values.yaml --wait
kubectl apply -k .
echo "모니터링 준비됨 — Grafana: kubectl -n monitoring port-forward svc/kps-grafana 3000:80"
