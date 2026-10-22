#!/usr/bin/env bash
# trading 의 Secret 을 만든다 — Mac Studio(trading-macstudio)에서 손으로 실행한다. 여러 번 실행해도 된다.
#   bash ~/Project/homelab/apps/trading/secrets.sh <claude.env 경로> <kis.env 경로> [toss.env 경로]
#   bash ~/Project/homelab/apps/trading/secrets.sh --stockdata <stockdata.env 경로>   (tradelab-stockdata 하나만, S5D)
# 파일에 들어 있는 변수(이름만):
#   claude.env  CLAUDE_CODE_OAUTH_TOKEN
#   kis.env     KIS_PAPER_APP_KEY  KIS_PAPER_APP_SECRET  (선택: KIS_PAPER_BASE_URL — 모의투자 도메인만)
#   toss.env    TOSS_CLIENT_ID  TOSS_CLIENT_SECRET  (선택 — 토스증권 조회 전용, 주는 경우에만 tradelab-toss 를 만든다)
#   stockdata.env  TIINGO_API_KEY  ALPHAVANTAGE_API_KEY  SEC_USER_AGENT  STOCKDATA_PSEUDONYM_KEY  (개별 주식 무료 소스 · 가명 키, --stockdata)
# ⭐ 값은 파일에서 바로 Secret 으로 간다(파일 전체가 키 하나가 된다) — 화면 · 저장소 · 로그에 남지 않는다.
# 바꾼 뒤에는 새 Job 을 시작해야 반영된다(Secret 은 파드가 뜰 때 읽는다). 이 Secret 은 배치 파드만 마운트한다(lab 은 아무것도 마운트하지 않는다)
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
NS=trading

if [ "${1:-}" = "--stockdata" ]; then
  if [ "$#" -ne 2 ] || [ ! -f "$2" ] || [ ! -s "$2" ]; then
    echo "사용법: bash $0 --stockdata <stockdata.env 경로> (파일이 없거나 비어 있다)" >&2
    exit 2
  fi
  kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS" >/dev/null
  # 키 이름은 파드 안의 파일 이름이다: /secrets/stockdata/stockdata.env
  kubectl -n "$NS" create secret generic tradelab-stockdata --from-file=stockdata.env="$2" --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
  echo "tradelab-stockdata ← stockdata.env"
  exit 0
fi

if [ "$#" -ne 2 ] && [ "$#" -ne 3 ]; then
  echo "사용법: bash $0 <claude.env 경로> <kis.env 경로> [toss.env 경로]" >&2
  exit 2
fi
CLAUDE_ENV=$1
KIS_ENV=$2
TOSS_ENV=${3:-}
for f in "$CLAUDE_ENV" "$KIS_ENV" ${TOSS_ENV:+"$TOSS_ENV"}; do
  if [ ! -f "$f" ] || [ ! -s "$f" ]; then
    echo "파일이 없거나 비어 있다: $f" >&2
    exit 1
  fi
done

kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS" >/dev/null
# 키 이름은 파드 안의 파일 이름이다: /secrets/claude/claude.env · /secrets/kis/kis.env · /secrets/toss/toss.env
kubectl -n "$NS" create secret generic tradelab-claude --from-file=claude.env="$CLAUDE_ENV" --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
echo "tradelab-claude ← claude.env"
kubectl -n "$NS" create secret generic tradelab-kis --from-file=kis.env="$KIS_ENV" --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
echo "tradelab-kis ← kis.env"
if [ -n "$TOSS_ENV" ]; then
  kubectl -n "$NS" create secret generic tradelab-toss --from-file=toss.env="$TOSS_ENV" --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
  echo "tradelab-toss ← toss.env"
fi
