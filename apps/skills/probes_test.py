import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PROBES = Path(__file__).resolve().parent / 'probes.sh'

# kubectl 대역: get 은 인자를 이은 키의 파일을, auth can-i 는 그 답(없으면 yes)을, exec 는 정해 둔 출력과 종료 코드를 낸다. 쓰기 명령은 없다.
STUB = r'''#!/usr/bin/env bash
# 파일 이름 = 인자를 공백 대신 _ 로 이은 키. 쓰기 명령은 없다.
d=$STUB_DIR
echo "$*" >> "$d/calls"
key() { local out=; for a in "$@"; do [ "$a" = -o ] || [ "$a" = json ] || out="$out${out:+_}${a//\//_}"; done; printf '%s' "$out"; }
case "$1" in
  get) f=$d/get/$(key "${@:2}"); [ -f "$f" ] && cat "$f" ;;
  auth) shift 2; args=(); for a in "$@"; do [ "$a" = --as ] && break; args+=("$a"); done
        f=$d/can_i/$(key "${args[@]}"); if [ -f "$f" ]; then cat "$f"; else echo yes; fi; [ "$(cat "$f" 2>/dev/null || echo yes)" = yes ] ;;
  -n) case " $* " in *' deploy/web '*) cat >/dev/null; echo 'PASS web 대역'; exit 0 ;; esac
      cat "$d/pod_exec_out"; exit "$(cat "$d/pod_exec_code")" ;;
  *) exit 2 ;;
esac
'''


def kustomize(path):
    rendered = subprocess.run(['kubectl', 'kustomize', path], cwd=ROOT, text=True, capture_output=True, check=True).stdout
    lines = subprocess.run(['yq', '-o=json', '-I=0', '.'], input=rendered, text=True, capture_output=True, check=True).stdout.splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def stub_key(key):
    return key.replace(' ', '_').replace('/', '_')


def listing(objects):
    return {'items': objects}


def healthy_cluster():
    runner = kustomize('apps/skills-runner')
    skills = kustomize('apps/skills')
    pick = lambda docs, kind, name: next(d for d in docs if d['kind'] == kind and d['metadata']['name'] == name)
    data = {
        'ns skills-runner': pick(runner, 'Namespace', 'skills-runner'),
        '-n skills-runner sa runner': pick(runner, 'ServiceAccount', 'runner'),
        'rolebinding -A': listing([]),
        # 실제 클러스터에도 있는 기본 바인딩(system:serviceaccounts 그룹)이 오탐이 되지 않아야 한다.
        'clusterrolebinding': listing([{'metadata': {'name': 'cluster-admin'}, 'subjects': [{'kind': 'User', 'name': 'someone-else'}]},
                                       {'metadata': {'name': 'system:service-account-issuer-discovery'}, 'subjects': [{'kind': 'Group', 'name': 'system:serviceaccounts'}]}]),
        '-n skills-runner rolebinding': listing([]),
        '-n skills-runner role': listing([]),
        '-n skills-runner networkpolicy': listing([d for d in runner if d['kind'] == 'NetworkPolicy']),
        '-n skills networkpolicy web-internal-ingress': pick(skills, 'NetworkPolicy', 'web-internal-ingress'),
        '-n skills svc web-internal': pick(skills, 'Service', 'web-internal'),
        '-n skills svc web': pick(skills, 'Service', 'web'),
        'ingress -A': listing([d for d in skills if d['kind'] == 'Ingress']),
        '-n skills deploy web': pick(skills, 'Deployment', 'web'),
        '-n skills-runner pod -l app=runner': listing([]),
    }
    can_i = {
        'get secrets -n skills-runner': 'no', 'get secrets -n skills': 'no',
        'create pods/exec -n skills-runner': 'no', 'list pods -n skills-runner': 'no',
    }
    return {'get': data, 'can_i': can_i, 'pod_exec': {'out': 'PASS 러너 대역', 'code': 0}}


class ProbesRunnerSetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ('kubectl', 'yq'):
            if not shutil.which(tool):
                raise RuntimeError(f'필수 도구 없음: {tool}')
        cls.base = healthy_cluster()

    def run_probes(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'bin').mkdir()
            stub = tmp / 'bin' / 'kubectl'
            stub.write_text(STUB)
            stub.chmod(0o755)
            for group in ('get', 'can_i'):
                (tmp / group).mkdir()
            for key, value in data['get'].items():
                (tmp / 'get' / stub_key(key)).write_text(json.dumps(value))
            for key, value in data['can_i'].items():
                (tmp / 'can_i' / stub_key(key)).write_text(value)
            (tmp / 'pod_exec_out').write_text(data['pod_exec']['out'])
            (tmp / 'pod_exec_code').write_text(str(data['pod_exec']['code']))
            env = {**os.environ, 'PATH': f'{tmp / "bin"}:{os.environ["PATH"]}', 'STUB_DIR': str(tmp), 'KUBECONFIG': str(tmp / 'no-such-kubeconfig')}
            result = subprocess.run(['bash', str(PROBES)], env=env, text=True, capture_output=True)
            # 대역이 실제로 불렸는지 — 진짜 kubectl 이 가려졌다면 이 기록이 비어 있다.
            self.assertTrue((tmp / 'calls').read_text().count('\n') > 10)
            return result

    def variant(self, mutate):
        data = copy.deepcopy(self.base)
        mutate(data)
        return data

    def failures(self, result):
        return [line for line in result.stdout.splitlines() if line.startswith('FAIL')]

    def test_rendered_manifests_pass_every_runner_check(self):
        # given
        data = self.base
        # when
        result = self.run_probes(data)
        # then
        self.assertEqual(self.failures(result), [])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('SKIP 러너 파드 없음', result.stdout)
        self.assertGreaterEqual(result.stdout.count('PASS'), 18)

    def test_a_binding_to_the_runner_service_account_fails(self):
        # given
        subject = {'kind': 'ServiceAccount', 'name': 'runner', 'namespace': 'skills-runner'}
        data = self.variant(lambda d: d['get'].update({'rolebinding -A': listing([{'metadata': {'name': 'x', 'namespace': 'skills'}, 'subjects': [subject]}])}))
        # when
        result = self.run_probes(data)
        # then
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.failures(result), ['FAIL SA runner 에 바인딩 없음 개수=1'])

    def test_a_local_rolebinding_or_a_granted_permission_fails(self):
        # given
        bound = self.variant(lambda d: d['get'].update({'-n skills-runner rolebinding': listing([{'metadata': {'name': 'x'}}])}))
        granted = self.variant(lambda d: d['can_i'].update({'get secrets -n skills': 'yes'}))
        # when
        results = [self.run_probes(bound), self.run_probes(granted)]
        # then
        self.assertEqual(self.failures(results[0]), ['FAIL skills-runner 에 RoleBinding 없음'])
        self.assertEqual(self.failures(results[1]), ['FAIL SA runner Secret 읽기(skills) 거부'])

    def test_automount_policy_ingress_and_service_drift_fail(self):
        # given
        def automount(d):
            d['get']['-n skills-runner sa runner'].pop('automountServiceAccountToken')
        def open_ingress(d):
            d['get']['-n skills-runner networkpolicy']['items'][0]['spec']['ingress'] = [{'from': [{'podSelector': {}}]}]
        def widen_8001(d):
            d['get']['-n skills networkpolicy web-internal-ingress']['spec']['ingress'][0]['from'] = [{'podSelector': {'matchLabels': {'app': 'runner'}}}]
        def ingress_to_internal(d):
            d['get']['ingress -A']['items'][0]['spec']['rules'][0]['http']['paths'][0]['backend']['service']['name'] = 'web-internal'
        def public_8001(d):
            d['get']['-n skills svc web']['spec']['ports'].append({'port': 8001})
        expected = {
            'automount': (automount, 'FAIL SA runner 토큰 자동 마운트 꺼짐'),
            'open_ingress': (open_ingress, 'FAIL skills-runner 정책에 ingress 허용 없음'),
            'widen_8001': (widen_8001, 'FAIL web 8001 ingress 는 skills-runner app=runner 에만'),
            'ingress_to_internal': (ingress_to_internal, 'FAIL Ingress 에 web-internal · 8001 없음'),
            'public_8001': (public_8001, 'FAIL Service web 은 8001 을 노출하지 않음'),
        }
        for name, (mutate, line) in expected.items():
            with self.subTest(case=name):
                # when
                result = self.run_probes(self.variant(mutate))
                # then
                self.assertIn(line, self.failures(result))
                self.assertNotEqual(result.returncode, 0)

    def test_extra_egress_or_a_web_secret_mount_fails(self):
        # given
        def internet(d):
            for p in d['get']['-n skills-runner networkpolicy']['items']:
                if p['metadata']['name'] == 'runner-to-web-internal':
                    p['spec']['egress'].append({'to': [{'ipBlock': {'cidr': '0.0.0.0/0'}}], 'ports': [{'port': 443}]})
        def allow_all(d):
            d['get']['-n skills-runner networkpolicy']['items'].append({'metadata': {'name': 'extra'}, 'spec': {'podSelector': {}, 'policyTypes': ['Egress'], 'egress': [{}]}})
        def user_subject(d):
            d['get']['clusterrolebinding']['items'].append({'metadata': {'name': 'y'}, 'subjects': [{'kind': 'User', 'name': 'system:serviceaccount:skills-runner:runner'}]})
        def token(d):
            d['get']['-n skills deploy web']['spec']['template']['spec']['volumes'].append({'name': 'c', 'secret': {'secretName': 'runner-claude'}})
        # when
        wide = self.run_probes(self.variant(internet))
        mounted = self.run_probes(self.variant(token))
        open_all = self.run_probes(self.variant(allow_all))
        user = self.run_probes(self.variant(user_subject))
        # then
        self.assertEqual(self.failures(open_all), ['FAIL 나가는 길 허용 = kube-dns:53, web:8001 뿐'])
        self.assertEqual(self.failures(user), ['FAIL SA runner 에 바인딩 없음 개수=1'])
        self.assertEqual(self.failures(wide), ['FAIL 나가는 길 허용 = kube-dns:53, web:8001 뿐'])
        self.assertEqual(self.failures(mounted), ['FAIL web 이 마운트하는 Secret 은 runner-hmac 뿐'])

    def test_backend_checks_use_fields_not_substrings(self):
        # given
        def noisy_metadata(d):
            d['get']['ingress -A']['items'][0]['metadata']['uid'] = 'a8001b-web-internal'
        def by_port(d):
            d['get']['ingress -A']['items'][0]['spec']['rules'][0]['http']['paths'][0]['backend']['service']['port'] = {'number': 8001}
        def other_namespace(d):
            item = d['get']['ingress -A']['items'][0]
            item['metadata']['namespace'] = 'other'
            item['spec']['rules'][0]['http']['paths'][0]['backend']['service']['port'] = {'number': 8001}
        def system_group(d):
            d['get']['clusterrolebinding']['items'].append({'metadata': {'name': 'x'}, 'subjects': [{'kind': 'Group', 'name': 'system:serviceaccounts:skills-runner'}]})
        # when
        noisy = self.run_probes(self.variant(noisy_metadata))
        port = self.run_probes(self.variant(by_port))
        other = self.run_probes(self.variant(other_namespace))
        group = self.run_probes(self.variant(system_group))
        # then
        self.assertEqual(self.failures(noisy), [])
        self.assertIn('FAIL Ingress 에 web-internal · 8001 없음', self.failures(port))
        self.assertEqual(self.failures(other), [])
        self.assertIn('FAIL SA runner 에 바인딩 없음 개수=1', self.failures(group))

    def test_running_runner_pod_is_probed_and_its_failure_propagates(self):
        # given
        pod = {'metadata': {'name': 'runner-0'}, 'status': {'phase': 'Running'}}
        good = self.variant(lambda d: d['get'].update({'-n skills-runner pod -l app=runner': listing([pod])}))
        bad = self.variant(lambda d: (d['get'].update({'-n skills-runner pod -l app=runner': listing([pod])}), d['pod_exec'].update({'out': 'FAIL 러너 호스트 SSH 연결 차단', 'code': 1})))
        # when
        passed = self.run_probes(good)
        failed = self.run_probes(bad)
        # then
        self.assertEqual(passed.returncode, 0, passed.stdout)
        self.assertIn('PASS 러너 파드 runner-0 안 검사 통과', passed.stdout)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('FAIL 러너 파드 runner-0 안 검사 통과', failed.stdout)
