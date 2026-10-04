#!/usr/bin/env bash
# 모니터링을 올린다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/monitoring/install.sh
# ⭐ 나중에 Argo CD 가 이 차트 · 값을 그대로 넘겨받는다. 버전은 여기서 고정한다.
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
cd "$(dirname "$0")"

KPS_VERSION=91.9.0
BLACKBOX_VERSION=11.19.1

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
kubectl apply -f probes.yaml -f rules.yaml
echo "모니터링 준비됨 — Grafana: kubectl -n monitoring port-forward svc/kps-grafana 3000:80"
