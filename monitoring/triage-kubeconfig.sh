#!/usr/bin/env bash
# 점검 에이전트용 kubeconfig 를 만든다 — Mac Studio 에서 한 번 실행한다(관리자 kubeconfig 필요). 여러 번 실행해도 된다.
#   bash ~/Project/homelab/monitoring/triage-kubeconfig.sh
# 결과: ~/.kube/triage.yaml (600) — 읽기 전용 계정 triage-reader(monitoring/triage-reader.yaml)의 토큰만 담는다
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
ADMIN=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
OUT=$HOME/.kube/triage.yaml
k() { kubectl --kubeconfig "$ADMIN" "$@"; }

token=$(k -n monitoring get secret triage-reader-token -o jsonpath='{.data.token}' | base64 -d)
[ -n "$token" ] || { echo "토큰이 아직 없다 — Argo 가 monitoring/triage-reader.yaml 을 적용했는지 본다" >&2; exit 1; }
server=$(k config view --minify -o jsonpath='{.clusters[0].cluster.server}')
ca=$(k config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

umask 077
cat > "$OUT.tmp" <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: homelab
    cluster: { server: "$server", certificate-authority-data: "$ca" }
users:
  - name: triage-reader
    user: { token: "$token" }
contexts:
  - name: triage
    context: { cluster: homelab, user: triage-reader, namespace: monitoring }
current-context: triage
YAML
mv "$OUT.tmp" "$OUT"
# 확인: 프로메테우스는 되고 Secret 목록은 안 돼야 한다
kubectl --kubeconfig "$OUT" get --raw /api/v1/namespaces/monitoring/services/kps-kube-prometheus-stack-prometheus:9090/proxy/api/v1/status/buildinfo >/dev/null
if kubectl --kubeconfig "$OUT" -n monitoring get secrets >/dev/null 2>&1; then echo "⚠️ Secret 이 읽힌다 — 권한이 넓다" >&2; exit 1; fi
echo "점검용 kubeconfig 준비됨 → $OUT"
