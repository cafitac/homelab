#!/usr/bin/env bash
# web Pod 시작 후 최소 2 분 뒤 실행한다(egress 허용 규칙 지연).
# AC3 앱 검증과 AC4 격리를 읽기만 한다. egress 적용은 192.168.5.2 와 Kubernetes API 검사만 증명한다.
# (3)은 UNVERIFIED 항목인 192.168.5.2 egress 차단을 해소한다.
# 이름 해석 실패는 연결 차단과 구분해 실패로 보고한다.
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
kubectl -n skills exec -i deploy/web -- python - <<'PY'
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import urllib.request
import urllib.error

failed = 0

def check(name, ok):
    global failed
    print(('PASS ' if ok else 'FAIL ') + name)
    failed += not ok


def status(path, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000' + path, headers=headers or {}), timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None

check('healthz', status('/healthz') == 200)
check('JWT 없음', status('/') == 403)
check('잘못된 JWT', status('/', {'Cf-Access-Jwt-Assertion': 'not-a-jwt'}) == 403)
paths = ['/var/run/secrets/kubernetes.io/serviceaccount', '~/.ssh', '~/.git-credentials', '~/.config/gh', '~/.netrc']
check('자격 파일 없음', not any(Path(p).expanduser().exists() for p in paths))
check('git 없음', shutil.which('git') is None)
names = [n for n in os.environ if not n.startswith('KUBERNETES_') and any(x in n.upper() for x in ('GIT', 'SSH', 'TOKEN', 'GH_'))]
check('민감 환경 이름 개수=' + str(len(names)), not names)
for name, host, port in [('호스트 SSH', '192.168.5.2', 22), ('호스트 모델', '192.168.5.2', 8800), ('API', os.environ.get('KUBERNETES_SERVICE_HOST'), 443), ('다른 앱(ppt 쪽 격리도 있음)', 'web.ppt.svc.cluster.local', 80)]:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM) if host else []
    except OSError:
        addresses = []
    if not addresses:
        check(name + ' 이름 해석', False)
        continue
    blocked = True
    for _, _, _, _, address in addresses:
        try:
            with socket.create_connection(address[:2], timeout=3):
                blocked = False
            break
        except OSError:
            pass
    check(name + ' 연결 차단', blocked)
try:
    with urllib.request.urlopen('https://' + os.environ['SKILLS_ACCESS_TEAM_DOMAIN'] + '/cdn-cgi/access/certs', timeout=10) as response:
        ok = response.status == 200 and 'keys' in json.load(response)
except Exception:
    ok = False
check('Access 인증서', ok)
sys.exit(bool(failed))
PY
