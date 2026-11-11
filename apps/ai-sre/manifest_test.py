# 렌더된 매니페스트가 격리 약속을 지키는지 본다(클러스터에 닿지 않는다). 실행: python3 -m unittest apps/ai-sre/manifest_test.py
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
ANTHROPIC = {'160.79.104.0/23', '153.61.224.0/22', '2607:6bc0::/48'}  # https://platform.claude.com/docs/en/api/ip-addresses


def select(policy, labels):
    return all(labels.get(k) == v for k, v in policy['spec']['podSelector'].get('matchLabels', {}).items())


def egress_ip_allows(docs, labels, ip, port):
    """labels 인 파드가 ip:port 로 나갈 수 있나 — 선택하는 Egress 정책의 ipBlock 규칙 합산."""
    address = ipaddress.ip_address(ip)
    for p in docs:
        if p['kind'] != 'NetworkPolicy' or 'Egress' not in p['spec']['policyTypes'] or not select(p, labels):
            continue
        for rule in p['spec'].get('egress', []):
            if port not in [x['port'] for x in rule.get('ports', [])]:
                continue
            for peer in rule.get('to', []):
                block = peer.get('ipBlock')
                if block and address in ipaddress.ip_network(block['cidr']) and not any(address in ipaddress.ip_network(n) for n in block.get('except', [])):
                    return True
    return False


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ('kubectl', 'yq'):
            if not shutil.which(tool):
                raise RuntimeError(f'필수 도구 없음: {tool}')
        out = subprocess.run(['kubectl', 'kustomize', 'apps/ai-sre'], cwd=ROOT, text=True, capture_output=True, check=True).stdout
        js = subprocess.run(['yq', '-o=json', '-I=0', '.'], input=out, text=True, capture_output=True, check=True).stdout
        cls.docs = [json.loads(line) for line in js.splitlines() if line.strip()]
        cls.policies = {d['metadata']['name']: d for d in cls.docs if d['kind'] == 'NetworkPolicy'}

    def test_everything_is_in_ai_sre_and_no_secret_is_committed(self):
        self.assertNotIn('Secret', {d['kind'] for d in self.docs})
        for d in self.docs:
            if d['kind'] != 'Namespace':
                self.assertEqual(d['metadata'].get('namespace'), 'ai-sre', d['metadata']['name'])

    def test_secret_refs_are_names_only(self):
        refs = set()
        def walk(o):
            if isinstance(o, dict):
                if 'secretKeyRef' in o:
                    refs.add(o['secretKeyRef']['name'])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(self.docs)
        self.assertEqual(refs, {'ai-sre-db'})

    def test_default_deny_both_directions_and_dns_only_opening_for_all(self):
        deny = self.policies['default-deny']['spec']
        self.assertEqual((deny['podSelector'], sorted(deny['policyTypes'])), ({}, ['Egress', 'Ingress']))
        self.assertEqual(self.policies['allow-dns']['spec']['podSelector'], {})

    def test_sandbox_reaches_only_anthropic_on_443(self):
        docs = list(self.policies.values())
        sandbox = {'app': 'sandbox'}
        cidrs = {peer['ipBlock']['cidr'] for p in docs if p['metadata']['name'] == 'sandbox-egress' for r in p['spec']['egress'] for peer in r['to']}
        self.assertEqual(cidrs, ANTHROPIC)
        self.assertTrue(egress_ip_allows(docs, sandbox, '160.79.104.10', 443))
        self.assertTrue(egress_ip_allows(docs, sandbox, '2607:6bc0::1', 443))
        for ip, port in [('160.79.104.10', 80), ('10.43.0.1', 443), ('192.168.5.2', 443), ('140.82.112.3', 443), ('10.42.0.5', 5432)]:
            self.assertFalse(egress_ip_allows(docs, sandbox, ip, port), (ip, port))
        sandbox_egress = [p for p in docs if 'Egress' in p['spec']['policyTypes'] and select(p, sandbox)]
        self.assertEqual({p['metadata']['name'] for p in sandbox_egress}, {'default-deny', 'allow-dns', 'sandbox-egress'})

    def test_sandbox_takes_only_worker_and_db_takes_only_its_clients(self):
        ing = self.policies['sandbox-ingress']['spec']['ingress']
        self.assertEqual([r['from'] for r in ing], [[{'podSelector': {'matchLabels': {'app': 'worker'}}}]])
        db = self.policies['db-ingress']['spec']['ingress'][0]['from'][0]['podSelector']['matchExpressions'][0]
        self.assertEqual(sorted(db['values']), ['api', 'backup', 'worker'])
        self.assertNotIn('sandbox', db['values'])

    def test_worker_internet_egress_blocks_private_ranges(self):
        docs = list(self.policies.values())
        worker = {'app': 'worker'}
        self.assertTrue(egress_ip_allows(docs, worker, '140.82.112.3', 443))
        for ip in ['10.43.0.1', '192.168.5.2', '172.16.0.1', '169.254.169.254', '100.64.0.1']:
            self.assertFalse(egress_ip_allows(docs, worker, ip, 443), ip)

    def test_volumes_survive_deletion_and_backup_is_scheduled(self):
        classes = [d['spec']['volumeClaimTemplates'][0]['spec']['storageClassName'] for d in self.docs if d['kind'] == 'StatefulSet']
        classes += [d['spec']['storageClassName'] for d in self.docs if d['kind'] == 'PersistentVolumeClaim']
        self.assertEqual(classes, ['local-path-retain'] * 2)
        cron = next(d for d in self.docs if d['kind'] == 'CronJob')
        self.assertEqual((cron['spec']['schedule'], cron['spec']['concurrencyPolicy']), ('40 4 * * *', 'Forbid'))
        self.assertEqual(cron['spec']['jobTemplate']['spec']['template']['metadata']['labels'], {'app': 'backup'})


if __name__ == '__main__':
    unittest.main()
