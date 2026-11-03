import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


def parse_yaml(text):
    result = subprocess.run(['yq', '-o=json', '-I=0', '.'], input=text, text=True, capture_output=True, check=True)
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def egress_allows(policies, ip, port, proto):
    address = ipaddress.ip_address(ip)
    for policy in policies:
        for rule in policy['spec'].get('egress', []):
            if not any(p['port'] == port and p.get('protocol', 'TCP') == proto for p in rule.get('ports', [])):
                continue
            for peer in rule.get('to', []):
                block = peer.get('ipBlock')
                if block and address in ipaddress.ip_network(block['cidr']) and not any(address in ipaddress.ip_network(n) for n in block.get('except', [])):
                    return True
    return False


def quantity(value):
    value = str(value)
    for suffix, scale in [('Gi', 1024**3), ('Mi', 1024**2), ('m', .001)]:
        if value.endswith(suffix):
            return float(value[:-len(suffix)]) * scale
    return float(value)


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ('kubectl', 'yq'):
            if not shutil.which(tool):
                raise RuntimeError(f'필수 도구 없음: {tool}')
        cls.rendered = subprocess.run(['kubectl', 'kustomize', 'apps/skills'], cwd=ROOT, text=True, capture_output=True, check=True).stdout
        cls.docs = parse_yaml(cls.rendered)
        cls.objects = {(d['kind'], d['metadata']['name']): d for d in cls.docs}
        cls.web = cls.objects['Deployment', 'web']['spec']['template']
        cls.job = cls.objects['CronJob', 'backup']['spec']['jobTemplate']['spec']['template']
        cls.templates = [cls.web, cls.job]
        cls.policies = [d for d in cls.docs if d['kind'] == 'NetworkPolicy' and 'Egress' in d['spec'].get('policyTypes', [])]

    def test_namespace_of_all_scoped_objects(self):
        # given
        scoped = [d for d in self.docs if d['kind'] != 'Namespace']
        # when
        namespaces = {d['metadata'].get('namespace') for d in scoped}
        # then
        self.assertEqual(namespaces, {'skills'})

    def test_pod_and_container_hardening(self):
        # given
        templates = self.templates
        # when
        specs = [t['spec'] for t in templates]
        # then
        for spec in specs:
            self.assertIs(spec['automountServiceAccountToken'], False)
            self.assertIs(spec['enableServiceLinks'], False)
            self.assertNotIn('serviceAccountName', spec)
            self.assertFalse(spec.get('hostNetwork'))
            self.assertFalse(spec.get('hostPID'))
            self.assertTrue(spec['securityContext']['runAsNonRoot'])
            self.assertEqual(spec['securityContext']['runAsUser'], 10001)
            self.assertEqual(spec['securityContext']['seccompProfile']['type'], 'RuntimeDefault')
            self.assertFalse(any('hostPath' in v for v in spec['volumes']))
            for container in spec['containers']:
                self.assertFalse(any('hostPort' in p for p in container.get('ports', [])))
                self.assertIs(container['securityContext']['allowPrivilegeEscalation'], False)
                self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])
                self.assertEqual(set(container['resources']), {'requests', 'limits'})

    def test_required_access_secret_for_both_containers(self):
        # given
        containers = [t['spec']['containers'][0] for t in self.templates]
        # when
        refs = [c['envFrom'] for c in containers]
        # then
        self.assertEqual(refs, [[{'secretRef': {'name': 'skills-access'}}]] * 2)

    def test_egress_selectors_and_dns(self):
        # given
        deny = self.objects['NetworkPolicy', 'default-deny-egress']['spec']
        web = self.objects['NetworkPolicy', 'web-egress']['spec']
        # when
        dns = web['egress'][0]
        # then
        self.assertEqual(len(self.policies), 2)
        self.assertEqual(deny, {'podSelector': {}, 'policyTypes': ['Egress']})
        self.assertEqual(web['podSelector'], {'matchLabels': {'app': 'web'}})
        self.assertNotEqual(self.job.get('metadata', {}).get('labels', {}).get('app'), 'web')
        self.assertEqual(dns['to'], [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}}, 'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}])
        self.assertEqual(dns['ports'], [{'port': 53, 'protocol': 'UDP'}, {'port': 53, 'protocol': 'TCP'}])

    def test_rendered_ipblock_connectivity_table(self):
        # given
        cases = [('192.168.5.2', 22, False), ('192.168.5.2', 8800, False), ('10.43.0.1', 443, False), ('10.42.1.5', 80, False), ('172.17.0.1', 443, False), ('169.254.169.254', 443, False), ('100.64.0.1', 443, False), ('8.8.8.8', 443, True), ('8.8.8.8', 80, False), ('fd00::1', 443, False), ('2606:4700::1111', 443, True)]
        # when
        results = [(ip, port, expected, egress_allows(self.policies, ip, port, 'TCP')) for ip, port, expected in cases]
        # then
        for ip, port, expected, actual in results:
            with self.subTest(ip=ip, port=port):
                self.assertEqual(actual, expected)

    def test_ingress_and_service_route(self):
        # given
        ingresses = [d for d in self.docs if d['kind'] == 'Ingress']
        # when
        rule = ingresses[0]['spec']['rules'][0]
        # then
        self.assertEqual(len(ingresses), 1)
        self.assertEqual(rule['host'], 'skills.cafitac.com')
        self.assertEqual(rule['http']['paths'][0], {'path': '/', 'pathType': 'Prefix', 'backend': {'service': {'name': 'web', 'port': {'number': 80}}}})
        self.assertEqual(self.objects['Service', 'web']['spec']['selector'], {'app': 'web'})

    def test_storage_and_quota_capacity(self):
        # given
        hard = self.objects['ResourceQuota', 'quota']['spec']['hard']
        # when
        containers = [t['spec']['containers'][0] for t in self.templates]
        # then
        for name, size in [('data', '10Gi'), ('backups', '5Gi')]:
            spec = self.objects['PersistentVolumeClaim', name]['spec']
            self.assertEqual(spec['storageClassName'], 'local-path-retain')
            self.assertEqual(spec['resources']['requests']['storage'], size)
            self.assertEqual(spec['accessModes'], ['ReadWriteOnce'])
        for category in ('requests', 'limits'):
            for resource in ('cpu', 'memory'):
                self.assertLessEqual(sum(quantity(c['resources'][category][resource]) for c in containers), quantity(hard[category + '.' + resource]))

    def test_image_placeholder_or_built_commit(self):
        # given
        pattern = r'^(unset-h2|[0-9a-f]{12})$'
        config = parse_yaml((ROOT / 'apps/skills/kustomization.yaml').read_text())[0]
        # when
        tag = config['images'][0]['newTag']
        # then
        self.assertRegex(tag, pattern)
        self.assertEqual([t['spec']['containers'][0]['image'] for t in self.templates], ['skills-dashboard:' + tag] * 2)

    def test_backup_schedule_mounts_and_embedded_script(self):
        # given
        cron = self.objects['CronJob', 'backup']['spec']
        # when
        mounts = self.job['spec']['containers'][0]['volumeMounts']
        # then
        self.assertEqual(cron['schedule'], '50 4 * * *')
        self.assertEqual(cron['timeZone'], 'Asia/Seoul')
        self.assertEqual(cron['concurrencyPolicy'], 'Forbid')
        self.assertEqual({m['name'] for m in mounts}, {'data', 'backups', 'backup-script'})
        self.assertEqual(self.objects['ConfigMap', 'backup-script']['data']['backup.py'], (ROOT / 'apps/skills/backup.py').read_text())

    def test_backup_runs_after_prune_failure_and_reports_either_failure(self):
        # given
        command = self.job['spec']['containers'][0]['command']
        shell, flag, script = command
        for prune, backup, expected in [(0, 0, 0), (2, 0, 2), (0, 3, 1), (2, 3, 1)]:
            with self.subTest(prune=prune, backup=backup):
                stub = ('python() { if [ "$1" = "-m" ]; then '
                        f'echo prune; return {prune}; else echo backup; return {backup}; fi; }}; ')
                # when
                result = subprocess.run(['sh', '-c', stub + script], text=True, capture_output=True)
                # then
                self.assertEqual(result.stdout.splitlines(), ['prune', 'backup'])
                self.assertEqual(result.returncode, expected)

    def test_argocd_retains_data_and_uses_repository_source(self):
        # given
        app = parse_yaml((ROOT / 'argocd/apps/skills.yaml').read_text())[0]
        ppt = parse_yaml((ROOT / 'argocd/apps/ppt.yaml').read_text())[0]
        # when
        spec = app['spec']
        # then
        self.assertEqual(spec['syncPolicy']['automated'], {'prune': False, 'selfHeal': True})
        self.assertEqual(spec['source']['path'], 'apps/skills')
        self.assertEqual(spec['source']['repoURL'], ppt['spec']['source']['repoURL'])
        self.assertNotIn('finalizers', app['metadata'])

    def test_offload_has_exactly_one_skills_target(self):
        # given
        script = (ROOT / 'cluster/backup-offload.sh').read_text()
        # when
        count = script.splitlines().count('  "skills:$OUT/skills"')
        # then
        self.assertEqual(count, 1)

    def test_published_sources_and_render_have_no_secret_material(self):
        # given
        files = [p for p in (ROOT / 'apps/skills').iterdir() if p.is_file()] + [ROOT / 'argocd/apps/skills.yaml']
        # when
        texts = [p.read_text() for p in files] + [self.rendered]
        forbidden = r'kind:\s*' + r'Secret\b|cloudflare' + r'access|[0-9a-fA-F]{32,}|PRIVATE' + r' KEY'
        # then
        for text in texts:
            self.assertIsNone(re.search(forbidden, text))
