#!/usr/bin/env bash
# 비공개 저장소를 Argo CD 가 읽게 한다 — 읽기 전용 배포 키(저장소 하나에만 유효)를 만들어 GitHub 과 클러스터에 넣는다.
#   bash argocd/set-repo-key.sh cafitac/thread-example     (gh 에 cafitac 계정이 로그인돼 있어야 한다)
# ⭐ 개인 키는 임시 디렉터리에서 만들어 Secret 으로만 넣고 지운다 — 저장소 · 화면에 남지 않는다.
#    다시 돌리면 같은 제목의 옛 키를 지우고 새 키로 바꾼다(키 교체).
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
REPO=${1:?owner/repo}
NAME=repo-$(echo "${REPO#*/}" | tr -c 'a-z0-9\n' '-')
TITLE="homelab argocd (read-only)"
export GH_TOKEN=${GH_TOKEN:-$(gh auth token --user cafitac)}

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
ssh-keygen -q -t ed25519 -N "" -C "$TITLE" -f "$tmp/key"

for id in $(gh api "repos/$REPO/keys" -q ".[] | select(.title == \"$TITLE\") | .id"); do
  gh api -X DELETE "repos/$REPO/keys/$id"
done
gh api "repos/$REPO/keys" -f title="$TITLE" -f key="$(cat "$tmp/key.pub")" -F read_only=true >/dev/null

kubectl -n argocd create secret generic "$NAME" \
  --from-literal=type=git --from-literal=url="git@github.com:$REPO.git" \
  --from-file=sshPrivateKey="$tmp/key" --dry-run=client -o yaml \
  | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml \
  | kubectl apply -f - >/dev/null
echo "배포 키(읽기 전용)를 $REPO 에 넣고 Secret argocd/$NAME 을 만들었다"
