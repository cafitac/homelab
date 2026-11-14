#!/usr/bin/env bash
# preview-hub 의 Secret 을 만든다 — Mac Studio 에서 실행한다. 여러 번 실행해도 된다.
# GitHub: README 의 PR bot 과 같은 fine-grained PAT — catalog 저장소만 허용하고
#   Pull requests R/W, Issues R/W, Contents R, Metadata R 권한을 준다.
#   토큰 원본은 소유자의 비밀번호 관리자에 보관한다.
#   PH-4 의 디렉터리 마운트 적용 후에는 교체한 토큰을 pod 재시작 없이 읽는다.
#   그 전에는 토큰 교체 후 pod 를 재시작해야 한다.
# Access: Cloudflare Zero Trust → Access → Applications → preview-hub 의 AUD tag,
#   팀 도메인은 Zero Trust 설정에서 가져온다.
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
NS=preview-hub

usage() {
  echo "사용법: $0 github <token-file|->" >&2
  echo "        $0 access <team-domain> <aud>" >&2
  exit 2
}

case "${1:-}" in
  github)
    [[ $# -eq 2 ]] || usage
    if [[ $2 == - ]]; then
      token=$(cat)
    elif [[ -r $2 ]]; then
      token=$(<"$2")
    else
      echo "GitHub 토큰 파일을 읽을 수 없다: $2" >&2
      exit 2
    fi
    if [[ -z ${token//[[:space:]]/} ]]; then
      echo "GitHub 토큰이 비어 있다." >&2
      exit 2
    fi
    kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
    # 값은 printf(셸 내장)로 stdin 에만 흘린다 — 프로세스 인자 · 임시 파일에 남지 않는다
    printf '%s\n' "$token" \
      | kubectl -n "$NS" create secret generic hub-github --from-file=github_token=/dev/stdin \
          --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    echo "hub-github 를 넣었다"
    ;;
  access)
    [[ $# -eq 3 ]] || usage
    if [[ ! $2 =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.cloudflareaccess\.com$ ||
          ! $3 =~ ^[a-fA-F0-9]{64}$ ]]; then
      echo "팀 도메인은 <name>.cloudflareaccess.com, AUD 는 64자리 16진수여야 한다." >&2
      exit 2
    fi
    kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
    # 팀 도메인과 AUD tag 는 Access 앱 설정에 보이는 값이므로 비밀이 아니다.
    kubectl -n "$NS" create secret generic hub-access \
      --from-literal="PHUB_ACCESS_TEAM_DOMAIN=$2" --from-literal="PHUB_ACCESS_AUD=$3" \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    echo "hub-access 를 넣었다"
    ;;
  *) usage ;;
esac
