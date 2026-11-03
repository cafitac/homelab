#!/usr/bin/env bash
# 전송 전에 owner 가 만든 묶음의 체크섬을 확인한다.
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
TAR=${1:?dashboard-cut-*.tar}
IFS= read -r SUM < "$TAR.sha256"
[[ "$SUM" =~ ^([[:xdigit:]]{64})\ \ (.+)$ ]] || { echo '체크섬 형식 오류' >&2; exit 1; }
SHA=${BASH_REMATCH[1]}
[ "${BASH_REMATCH[2]}" = "$(basename "$TAR")" ] || { echo '파일 이름 불일치' >&2; exit 1; }
ACTUAL=$(shasum -a 256 "$TAR" | cut -d' ' -f1)
[ "$SHA" = "$ACTUAL" ] || { echo '체크섬 불일치' >&2; exit 1; }
kubectl -n skills exec -i deploy/web -- python -m skills_web.admin import --sha256 "$SHA" < "$TAR"
