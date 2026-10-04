#!/usr/bin/env bash
# 폴더를 data PVC(/data)로 옮긴다 — Mac Studio 에서 실행한다.
#   bash load-data.sh <원본 폴더> <하위 폴더>...      예) load-data.sh /Volumes/TradingData/judge-board packs problems work
# 문제 꾸러미 갱신: judge-board 의 deploy/sync-packs.sh <스터디 저장소> <임시 폴더>(해설 검사 포함) → load-data.sh <임시 폴더> packs problems
# ⭐ 하위 폴더는 통째로 바꾼다(지우고 푼다) — 지난 꾸러미의 파일이 남으면 채점 원본이 섞인다
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
SRC=${1:?원본 폴더}; shift
[ $# -gt 0 ] || { echo "옮길 하위 폴더를 적는다" >&2; exit 1; }
NS=judge-board
kubectl -n "$NS" delete pod data-loader --ignore-not-found --wait=true >/dev/null
kubectl -n "$NS" run data-loader --image=busybox:1.37 --restart=Never --overrides='{
  "spec": {"containers": [{"name": "loader", "image": "busybox:1.37", "command": ["sleep", "600"],
    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
    "resources": {"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "128Mi"}}}],
  "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]}}' >/dev/null
kubectl -n "$NS" wait --for=condition=Ready pod/data-loader --timeout=120s >/dev/null
for d in "$@"; do
  kubectl -n "$NS" exec data-loader -- sh -c "rm -rf /data/$d && mkdir -p /data/$d"
done
tar -C "$SRC" -cf - "$@" | kubectl -n "$NS" exec -i data-loader -- tar -C /data -xf -
# board(java · uid 1001 안팎) · runner(root) · dockerd 가 모두 쓴다
kubectl -n "$NS" exec data-loader -- sh -c "chmod -R a+rwX /data && du -s $(printf '/data/%s ' "$@")"
kubectl -n "$NS" delete pod data-loader --wait=false >/dev/null
