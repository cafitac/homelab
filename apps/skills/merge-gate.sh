#!/usr/bin/env bash
# 태그 형식(소문자 16진수 12 자리)만 검사한다. Mac Studio 에서 실제 빌드·import 했다는 것은 owner 보고(머지 게이트 3)이며 이 스크립트가 증명하지 않는다.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
tag=$(yq -r '.images[] | select(.name == "skills-dashboard") | .newTag' "$script_dir/kustomization.yaml")
if [[ ! "$tag" =~ ^[0-9a-f]{12}$ ]]; then
  echo "머지 전 빌드된 12 자리 커밋 태그가 필요합니다." >&2
  exit 1
fi
printf '%s\n' "$tag"
