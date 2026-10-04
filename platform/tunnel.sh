#!/usr/bin/env bash
# 터널 homelab 을 만들고(없으면) 토큰을 Secret 으로 넣는다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/platform/tunnel.sh
# 호스트를 이 터널로 옮길 때: cloudflared tunnel route dns --overwrite-dns homelab <호스트>
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}

cloudflared tunnel list 2>/dev/null | awk '$2=="homelab"{f=1} END{exit !f}' || cloudflared tunnel create homelab
kubectl get ns platform >/dev/null 2>&1 || kubectl create ns platform
# 토큰은 파일 · 화면에 남기지 않고 바로 Secret 으로 넘긴다
cloudflared tunnel token homelab | kubectl -n platform create secret generic cloudflared-token \
  --from-file=token=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -
echo "터널 homelab 토큰을 Secret platform/cloudflared-token 으로 넣었다"
