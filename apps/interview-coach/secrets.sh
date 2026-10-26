#!/usr/bin/env bash
# interview-coach 의 Secret 을 만든다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다(db-auth 비밀번호는 처음 한 번만 만든다).
#   bash ~/Project/homelab/apps/interview-coach/secrets.sh
# ⭐ 값은 Mac Studio 의 .env 파일(compose 시절 그대로)에서 바로 Secret 으로 간다 — 화면 · 저장소에 남지 않는다.
#    DB 접속 정보(URL · 계정)는 매니페스트의 env 가 db-auth 로 덮어쓴다
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
SRC=${SRC:-$HOME/Project/interview-coach}
NS=interview-coach

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
apply_env() {  # $1 = Secret 이름, $2 = .env 파일
  kubectl -n "$NS" create secret generic "$1" --from-env-file="$2" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "$1 ← $2 ($(grep -c '=' "$2") 개)"
}
apply_env server-env "$SRC/server/.env"
apply_env knowledge-env "$SRC/knowledge/.env"
apply_env embedder-env "$SRC/embedder/.env"
# k8s 의 DB 비밀번호는 새로 만든다(compose 시절 값을 쓰지 않는다)
if ! kubectl -n "$NS" get secret db-auth >/dev/null 2>&1; then
  kubectl -n "$NS" create secret generic db-auth \
    --from-literal=POSTGRES_USER=coach --from-literal=POSTGRES_DB=coach \
    --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)" >/dev/null
  echo "db-auth 를 새로 만들었다"
fi
# Umami(umami.yaml) — DB 비밀번호 · 앱 비밀은 처음 한 번 무작위로 만든다
if ! kubectl -n "$NS" get secret umami-auth >/dev/null 2>&1; then
  pw=$(openssl rand -hex 24)
  kubectl -n "$NS" create secret generic umami-auth \
    --from-literal=POSTGRES_PASSWORD="$pw" \
    --from-literal=DATABASE_URL="postgresql://umami:$pw@umami-db:5432/umami" \
    --from-literal=APP_SECRET="$(openssl rand -hex 32)" >/dev/null
  echo "umami-auth 를 새로 만들었다"
fi
