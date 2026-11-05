import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
WORKLOADS = {'Deployment', 'StatefulSet', 'DaemonSet', 'ReplicaSet', 'Job', 'CronJob', 'Pod'}
RBAC = {'Role', 'RoleBinding', 'ClusterRole', 'ClusterRoleBinding'}


def parse_yaml(text):
    result = subprocess.run(['yq', '-o=json', '-I=0', '.'], input=text, text=True, capture_output=True, check=True)
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def render(path):
    rendered = subprocess.run(['kubectl', 'kustomize', path], cwd=ROOT, text=True, capture_output=True, check=True).stdout
    return rendered, parse_yaml(rendered)


class RunnerNamespaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ('kubectl', 'yq'):
            if not shutil.which(tool):
                raise RuntimeError(f'필수 도구 없음: {tool}')
        cls.rendered, cls.docs = render('apps/skills-runner')
        cls.objects = {(d['kind'], d['metadata']['name']): d for d in cls.docs}
        cls.policies = {d['metadata']['name']: d['spec'] for d in cls.docs if d['kind'] == 'NetworkPolicy'}

    def test_everything_lives_in_skills_runner(self):
        # given
        scoped = [d for d in self.docs if d['kind'] != 'Namespace']
        # when
        namespaces = {d['metadata'].get('namespace') for d in scoped}
        # then
        self.assertEqual(namespaces, {'skills-runner'})
        self.assertEqual(self.objects['Namespace', 'skills-runner']['metadata']['name'], 'skills-runner')

    def test_no_rbac_object_and_no_workload(self):
        # given
        kinds = {d['kind'] for d in self.docs}
        # when
        found = kinds & (RBAC | WORKLOADS)
        # then
        self.assertEqual(found, set())
        self.assertEqual(kinds, {'Namespace', 'ServiceAccount', 'ResourceQuota', 'NetworkPolicy'})

    def test_service_account_has_no_token_mount(self):
        # given
        sa = self.objects['ServiceAccount', 'runner']
        # when
        automount = sa['automountServiceAccountToken']
        # then
        self.assertIs(automount, False)
        self.assertNotIn('secrets', sa)
        self.assertNotIn('imagePullSecrets', sa)

    def test_default_deny_covers_both_directions_for_every_pod(self):
        # given
        deny = self.policies['default-deny']
        # when
        types = sorted(deny['policyTypes'])
        # then
        self.assertEqual(deny['podSelector'], {})
        self.assertEqual(types, ['Egress', 'Ingress'])
        self.assertNotIn('ingress', deny)
        self.assertNotIn('egress', deny)

    def test_no_policy_allows_any_ingress(self):
        # given
        specs = self.policies.values()
        # when
        opened = [s for s in specs if s.get('ingress')]
        # then
        self.assertEqual(opened, [])
        self.assertEqual([n for n, s in self.policies.items() if 'Ingress' in s['policyTypes']], ['default-deny'])

    def test_egress_is_dns_and_web_internal_only(self):
        # given
        dns = self.policies['allow-dns']
        runner = self.policies['runner-to-web-internal']
        # when
        peers = [(p, r.get('ports')) for s in self.policies.values() for r in s.get('egress', []) for p in r['to']]
        # then
        self.assertEqual(len(peers), 2)
        self.assertFalse(any('ipBlock' in p for p, _ in peers))
        self.assertEqual(dns['podSelector'], {})
        self.assertEqual(dns['egress'], [{
            'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}}, 'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
            'ports': [{'port': 53, 'protocol': 'UDP'}, {'port': 53, 'protocol': 'TCP'}]}])
        self.assertEqual(runner['podSelector'], {'matchLabels': {'app': 'runner'}})
        self.assertEqual(runner['egress'], [{
            'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'skills'}}, 'podSelector': {'matchLabels': {'app': 'web'}}}],
            'ports': [{'port': 8001, 'protocol': 'TCP'}]}])

    def test_quota_is_bounded_and_forbids_volumes_and_external_services(self):
        # given
        hard = self.objects['ResourceQuota', 'quota']['spec']['hard']
        # when
        keys = set(hard)
        # then
        for key in ('pods', 'requests.cpu', 'requests.memory', 'requests.ephemeral-storage', 'limits.cpu', 'limits.memory', 'limits.ephemeral-storage'):
            self.assertIn(key, keys)
        self.assertEqual(hard['persistentvolumeclaims'], '0')
        self.assertEqual(hard['services.loadbalancers'], '0')
        self.assertEqual(hard['services.nodeports'], '0')

    def test_argocd_application_is_registered_without_prune(self):
        # given
        app = parse_yaml((ROOT / 'argocd/apps/skills-runner.yaml').read_text())[0]
        skills = parse_yaml((ROOT / 'argocd/apps/skills.yaml').read_text())[0]
        # when
        spec = app['spec']
        # then
        self.assertEqual(app['metadata'], {'name': 'skills-runner', 'namespace': 'argocd'})
        self.assertEqual(spec['source']['path'], 'apps/skills-runner')
        self.assertEqual(spec['source']['repoURL'], skills['spec']['source']['repoURL'])
        self.assertEqual(spec['source']['targetRevision'], 'main')
        self.assertEqual(spec['syncPolicy']['automated'], {'prune': False, 'selfHeal': True})

    def test_published_files_hold_no_secret_material_or_account_names(self):
        # given
        files = [p for p in HERE.iterdir() if p.is_file() and p.name != Path(__file__).name and not p.name.endswith('_test.py')]
        # when
        texts = {p.name: p.read_text() for p in files}
        # then
        self.assertTrue({'REVOKE.md', 'secrets.sh', 'kustomization.yaml'} <= set(texts))
        forbidden = r'kind:\s*' + r'Secret\b|[0-9a-fA-F]{32,}|[A-Za-z0-9_=-]{40,}|PRIVATE' + r' KEY|@|cloudflare'
        for name, text in texts.items():
            with self.subTest(file=name):
                self.assertIsNone(re.search(forbidden, text, re.IGNORECASE))
        self.assertIsNone(re.search(r'kind:\s*' + r'Secret\b', self.rendered))

    def test_secrets_script_never_prints_or_traces_values(self):
        # given
        script = (HERE / 'secrets.sh').read_text()
        # when
        code = [line for line in script.splitlines() if not line.lstrip().startswith('#')]
        # then
        self.assertFalse(any(re.search(r'set\s+-\w*x|xtrace|--from-literal|\bcat\b|\bsource\b|^\s*\. ', line) for line in code))
        for line in code:
            if re.match(r'\s*echo\b', line):
                self.assertNotRegex(line, r'\$\{?(CLAUDE|GH_|RUNNER_HMAC|value|token|key)', msg=line)
        self.assertIn('ENV_FILE=${ENV_FILE:-/Volumes/TradingData/skills-runner/env}', script)
