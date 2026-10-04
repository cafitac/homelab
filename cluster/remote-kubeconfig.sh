#!/usr/bin/env bash
# 로컬 PC 에서 Mac Studio 클러스터를 kubectl 로 다루게 한다 — 로컬 PC 에서 실행한다.
#   bash cluster/remote-kubeconfig.sh     ~/.kube/homelab.yaml 을 만들고, API 를 ssh 로 끌어오는 명령을 알려 준다
#   KUBECONFIG=~/.kube/homelab.yaml kubectl get nodes
#
# ⭐ k8s API 는 도메인 · tailnet 에 열지 않는다. kubeconfig 의 인증서 하나가 클러스터 전체 권한이라
#    ssh 로 이미 인증된 사람만 쓰게 한다. API 인증서에 127.0.0.1 이 들어 있어 포워딩한 주소 그대로 검증된다.
set -euo pipefail
HOST=${HOST:-trading-macstudio}
mkdir -p "$HOME/.kube"
# Mac Studio 의 kubeconfig 를 그대로 쓴다 — 서버 주소가 127.0.0.1:<포트> 라 같은 포트를 로컬로 끌어오면 된다
ssh "$HOST" 'cat ~/.kube/homelab.yaml' > "$HOME/.kube/homelab.yaml.tmp"
mv "$HOME/.kube/homelab.yaml.tmp" "$HOME/.kube/homelab.yaml"
chmod 600 "$HOME/.kube/homelab.yaml"
PORT=$(sed -nE 's#.*server: https://127\.0\.0\.1:([0-9]+).*#\1#p' "$HOME/.kube/homelab.yaml")
if ! lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  ssh -fN -L "$PORT:127.0.0.1:$PORT" "$HOST"
fi
echo "준비됨 — KUBECONFIG=~/.kube/homelab.yaml kubectl get nodes (API 는 ssh 포워딩 127.0.0.1:$PORT)"
