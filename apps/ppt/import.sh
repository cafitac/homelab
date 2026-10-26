#!/usr/bin/env bash
# 외장 SSD 의 덱 폴더 하나를 ppt 의 data PVC 로 가져온다 — Mac Studio 에서 실행한다.
#   bash ~/Project/homelab/apps/ppt/import.sh <slug>          예: 3d-63building
# 원본(/Volumes/TradingData/ppt/decks/<slug>)은 읽기만 한다: tar 로 묶어 sha256 을 재고, web 파드의
# `python -m ppt_web.admin import` 로 흘려 보낸다. 앞뒤로 원본 폴더 체크섬을 비교해 바뀌지 않았는지 확인한다.
# 같은 slug 를 다시 가져오지는 않는다(앱이 거절한다).
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
SLUG=${1:?slug}
SRC=/Volumes/TradingData/ppt/decks/$SLUG
NS=ppt
[ -d "$SRC" ] || { echo "$SRC 가 없다" >&2; exit 1; }
tree_sum() { (cd "$SRC" && find . -type f -exec shasum -a 256 {} + | sort -k2 | shasum -a 256 | cut -d' ' -f1); }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
before=$(tree_sum)
tar -C "$SRC" --exclude .DS_Store -cf "$TMP/deck.tar" .
SHA=$(shasum -a 256 "$TMP/deck.tar" | cut -d' ' -f1)
kubectl -n "$NS" exec -i deploy/web -- python -m ppt_web.admin import "$SLUG" --sha256 "$SHA" --source-path "$SRC" < "$TMP/deck.tar"
after=$(tree_sum)
[ "$before" = "$after" ] && echo "원본 그대로 ($before)" || { echo "⚠️ 원본 체크섬이 바뀌었다: $before → $after" >&2; exit 1; }
