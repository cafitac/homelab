#!/usr/bin/env bash
# Purpose: AC12 사본 복원 가능성을 확인한다.
# Effect: 일일 Job 과 같은 prune 을 운영 DB 에 실행하고 외부 사본을 임시 폴더에서 읽는다.
# Success: 무결성이 ok 이고 세 표의 행 수가 운영 DB 와 같다.
# On failure: Job 로그와 skills 사본을 확인한다.
# Rerun: 임시 Job 과 폴더를 지우므로 다시 실행해도 된다.
# CPU 할당량 때문에 web 외 실행·대기 Pod 가 있으면 중단한다.
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
OUT=${OUT:-$HOME/Backups/k8s}
export OUT
echo 'namespace=skills cronjob=backup'
read -r -p '백업과 외부 복사 점검을 실행합니까? [yes] ' ANSWER
[ "$ANSWER" = yes ] || exit 1
BUSY_PODS=$(kubectl -n skills get pods --no-headers -o custom-columns=NAME:.metadata.name,PHASE:.status.phase | grep -Ev '^web-|Succeeded|Failed' || true)
[ -z "$BUSY_PODS" ] || { echo 'web 외 실행·대기 Pod 가 있어 복원 점검을 중단한다' >&2; exit 1; }
JOB=drill-$(date -u +%Y%m%dT%H%M%SZ | tr '[:upper:]' '[:lower:]')
TMP=$(mktemp -d)
trap 'kubectl -n skills delete job "$JOB" --ignore-not-found >/dev/null; rm -rf "$TMP"' EXIT
kubectl -n skills create job "$JOB" --from=cronjob/backup
kubectl -n skills wait --for=condition=complete --timeout=300s "job/$JOB"
bash "$(dirname "$0")/../../cluster/backup-offload.sh" || echo '다른 대상의 사본이 실패했다 — skills 사본만 확인한다' >&2
NAME=$(kubectl -n skills logs "job/$JOB" | sed -n 's/.*"backup": "\([^"]*\)".*/\1/p' | tail -1)
[ -n "$NAME" ] || { echo 'Job 로그에 덤프 이름이 없다' >&2; exit 1; }
LATEST=$OUT/skills/$NAME
[ -f "$LATEST" ] || { echo 'skills 외부 사본이 없다' >&2; exit 1; }
cp "$LATEST" "$TMP/skills.db"
[ "$(sqlite3 "$TMP/skills.db" 'PRAGMA integrity_check')" = ok ] || { echo '무결성 실패' >&2; exit 1; }
SQL="select (select count(*) from snapshot)||' '||(select count(*) from content_item)||' '||(select count(*) from schema_migrations)"
COPY=$(sqlite3 "$TMP/skills.db" "$SQL")
LIVE=$(kubectl -n skills exec -i deploy/web -- python - "$SQL" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path
with sqlite3.connect((Path(os.environ['SKILLS_DATA_DIR']) / 'skills.db').as_uri() + '?mode=ro', uri=True) as db:
    print(db.execute(sys.argv[1]).fetchone()[0])
PY
)
printf '사본: %s\n운영: %s\n' "$COPY" "$LIVE"
[ "$COPY" = "$LIVE" ]
