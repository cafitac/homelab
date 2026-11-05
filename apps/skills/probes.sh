#!/usr/bin/env bash
# web Pod 시작 후 최소 2 분 뒤 실행한다(egress 허용 규칙 지연).
# AC3 앱 검증과 AC4 격리를 읽기만 한다. egress 적용은 192.168.5.2 와 Kubernetes API 검사만 증명한다.
# (3)은 UNVERIFIED 항목인 192.168.5.2 egress 차단을 해소한다.
# 이름 해석 실패는 연결 차단과 구분해 실패로 보고한다.
# 마지막 구역은 AC23·AC24 러너 쪽(U21h) — kubectl 읽기만 한다(auth can-i 는 impersonation 을 쓰는 읽기 질의다). 러너 파드가 떠 있으면 거기서 차단도 확인한다.
set -euo pipefail
export PATH=$PATH:/opt/homebrew/bin
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/homelab.yaml}
web_status=0
kubectl -n skills exec -i deploy/web -- python - <<'PY' || web_status=$?
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

echo '--- 러너 (AC23·AC24)'
runner_status=0
python3 -I - <<'PY' || runner_status=$?
import json
import subprocess
import sys

failed = 0
SA = 'system:serviceaccount:skills-runner:runner'


def check(name, ok):
    global failed
    print(('PASS ' if ok else 'FAIL ') + name)
    failed += not ok


def kube(*args):
    return subprocess.run(['kubectl', *args], text=True, capture_output=True)


def get(*args):
    result = kube('get', *args, '-o', 'json')
    return json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else None


def can_i(*args):
    # 읽기 질의다. 거부(no)여야 통과이고, 질의 자체가 안 되면 실패로 본다.
    result = kube('auth', 'can-i', *args, '--as', SA)
    return result.stdout.strip()


ns = get('ns', 'skills-runner')
check('namespace skills-runner', ns is not None)
sa = get('-n', 'skills-runner', 'sa', 'runner')
check('SA runner 토큰 자동 마운트 꺼짐', sa is not None and sa.get('automountServiceAccountToken') is False)

def items(*args):
    data = get(*args)
    return None if data is None else data['items']


role_bindings = items('rolebinding', '-A')
cluster_bindings = items('clusterrolebinding')
check('바인딩 목록 읽기', role_bindings is not None and cluster_bindings is not None)
subjects = [s for b in (role_bindings or []) + (cluster_bindings or []) for s in b.get('subjects') or []]
mine = [s for s in subjects if (s.get('kind') == 'ServiceAccount' and s.get('name') == 'runner' and s.get('namespace') == 'skills-runner') or s.get('name') in ('system:serviceaccounts:skills-runner', SA)]
check('SA runner 에 바인딩 없음 개수=' + str(len(mine)), not mine)
local_bindings = items('-n', 'skills-runner', 'rolebinding')
check('skills-runner 에 RoleBinding 없음', local_bindings == [])
local_roles = items('-n', 'skills-runner', 'role')
check('skills-runner 에 Role 없음', local_roles == [])
for name, args in [('Secret 읽기', ('get', 'secrets', '-n', 'skills-runner')), ('Secret 읽기(skills)', ('get', 'secrets', '-n', 'skills')),
                   ('pods/exec', ('create', 'pods/exec', '-n', 'skills-runner')), ('pod 목록', ('list', 'pods', '-n', 'skills-runner'))]:
    check('SA runner ' + name + ' 거부', can_i(*args) == 'no')

policies = get('-n', 'skills-runner', 'networkpolicy')
by_name = {p['metadata']['name']: p['spec'] for p in (policies or {'items': []})['items']}
check('NetworkPolicy 읽기', bool(by_name))
deny = by_name.get('default-deny', {})
check('기본 거부 ingress+egress', deny.get('podSelector') == {} and sorted(deny.get('policyTypes', [])) == ['Egress', 'Ingress'] and not deny.get('ingress') and not deny.get('egress'))
check('skills-runner 정책에 ingress 허용 없음', not any(spec.get('ingress') for spec in by_name.values()))
allowed = []
for spec in by_name.values():
    for rule in spec.get('egress') or []:
        if not rule.get('to'):
            allowed.append('any')  # to 가 없거나 비면 모든 목적지다
        for peer in rule.get('to') or []:
            if 'ipBlock' in peer:
                allowed.append('ipBlock')
            else:
                labels = (peer.get('namespaceSelector') or {}).get('matchLabels', {})
                pods = (peer.get('podSelector') or {}).get('matchLabels', {})
                allowed.append((labels.get('kubernetes.io/metadata.name'), tuple(sorted(pods.items())), tuple(sorted(p['port'] for p in rule.get('ports', [])))))
# U21p(프록시)가 이 허용 목록을 바꾼다 — 그 PR 이 이 줄도 함께 고친다.
expected = [('kube-system', (('k8s-app', 'kube-dns'),), (53, 53)), ('skills', (('app', 'web'),), (8001,))]
check('나가는 길 허용 = kube-dns:53, web:8001 뿐', sorted(map(str, allowed)) == sorted(map(str, expected)))

web_policy = (get('-n', 'skills', 'networkpolicy', 'web-internal-ingress') or {}).get('spec', {})
rules = web_policy.get('ingress') or []
peer_ok = len(rules) == 1 and rules[0].get('from') == [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'skills-runner'}}, 'podSelector': {'matchLabels': {'app': 'runner'}}}]
check('web 8001 ingress 는 skills-runner app=runner 에만', peer_ok and rules[0].get('ports') == [{'port': 8001, 'protocol': 'TCP'}] and web_policy.get('podSelector') == {'matchLabels': {'app': 'web'}})

internal = get('-n', 'skills', 'svc', 'web-internal')
spec = (internal or {}).get('spec', {})
check('Service web-internal ClusterIP 8001', internal is not None and spec.get('type') == 'ClusterIP' and spec.get('selector') == {'app': 'web'} and [p['port'] for p in spec.get('ports', [])] == [8001])
public = (get('-n', 'skills', 'svc', 'web') or {}).get('spec', {})
check('Service web 은 8001 을 노출하지 않음', [p['port'] for p in public.get('ports', [])] == [80] and public.get('type', 'ClusterIP') == 'ClusterIP')
ingresses = get('ingress', '-A')
backend_services = []
for ingress in (ingresses or {'items': []})['items']:
    ingress_spec = ingress.get('spec', {})
    backends = [ingress_spec.get('defaultBackend')] + [path.get('backend') for rule in ingress_spec.get('rules') or [] for path in (rule.get('http') or {}).get('paths') or []]
    for backend in backends:
        service = (backend or {}).get('service')
        if service:
            backend_services.append((ingress['metadata'].get('namespace'), service))
# web-internal 을 가리키거나, skills namespace 의 Ingress 가 8001(internal)로 보내면 실패다.
internal_refs = [svc for namespace, svc in backend_services if svc.get('name') == 'web-internal' or (namespace == 'skills' and ((svc.get('port') or {}).get('number') == 8001 or (svc.get('port') or {}).get('name') == 'internal'))]
check('Ingress 에 web-internal · 8001 없음', ingresses is not None and not internal_refs)

web = (get('-n', 'skills', 'deploy', 'web') or {}).get('spec', {}).get('template', {}).get('spec', {})
secret_volumes = sorted(v['secret']['secretName'] for v in web.get('volumes', []) if 'secret' in v)
env_from = [e.get('secretRef', {}).get('name') for c in web.get('containers', []) for e in c.get('envFrom', [])]
check('web 이 마운트하는 Secret 은 runner-hmac 뿐', secret_volumes in ([], ['runner-hmac']) and set(env_from) <= {'skills-access'})

pods = items('-n', 'skills-runner', 'pod', '-l', 'app=runner') or []
running = [p['metadata']['name'] for p in pods if p['status'].get('phase') == 'Running']
if not running:
    print('SKIP 러너 파드 없음 — 파드 안 차단 확인은 U23d 이후')
else:
    code = """
import os, socket, sys
failed = 0
def reach(host, port):
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return 'noname'
    for _, _, _, _, address in addresses:
        try:
            with socket.create_connection(address[:2], timeout=3):
                return 'open'
        except OSError:
            pass
    return 'blocked'
def check(name, ok):
    global failed
    print(('PASS ' if ok else 'FAIL ') + name)
    failed += not ok
for name, host, port in [('호스트 SSH', '192.168.5.2', 22), ('호스트 모델', '192.168.5.2', 8800), ('API', os.environ.get('KUBERNETES_SERVICE_HOST'), 443), ('다른 앱', 'web.ppt.svc.cluster.local', 80), ('web 공개 포트', 'web.skills.svc.cluster.local', 80), ('인터넷 직접', '1.1.1.1', 443)]:
    check('러너 ' + name + ' 연결 차단', bool(host) and reach(host, port) == 'blocked')
# 차단은 시간 초과든 거부(kube-router 는 REJECT 를 쓴다)든 연결 실패로 보이므로 열린 길은 연결이 되어야만 통과다. 웹 이미지가 8001 을 열기 전(U21b 이전)에는 이 항목이 실패한다.
check('러너 web-internal:8001 연결', reach('web-internal.skills.svc.cluster.local', 8001) == 'open')
sys.exit(bool(failed))
"""
    for pod in running:
        result = kube('-n', 'skills-runner', 'exec', pod, '-c', 'agent', '--', 'python', '-c', code)
        sys.stdout.write(result.stdout)
        check('러너 파드 ' + pod + ' 안 검사 통과', result.returncode == 0 and 'PASS' in result.stdout)
sys.exit(bool(failed))
PY
exit $((web_status || runner_status))
