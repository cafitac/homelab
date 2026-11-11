# 렌더된 매니페스트가 격리 약속을 지키는지 본다(클러스터에 닿지 않는다). 실행: python3 -m unittest apps/ai-sre/manifest_test.py
# NetworkPolicy 는 쿠버네티스 의미 그대로 모형화한다: 정책은 합산(더하기만), ports 생략 = 모든 포트, to/from 생략 · 빈 peer = 모든 상대.
import copy
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
ANTHROPIC = {'160.79.104.0/23', '153.61.224.0/22', '2607:6bc0::/48'}  # https://platform.claude.com/docs/en/api/ip-addresses
NS = {'kubernetes.io/metadata.name': 'ai-sre'}
KUBE_DNS = ('pod', {'kubernetes.io/metadata.name': 'kube-system'}, {'k8s-app': 'kube-dns'})


def pod(app, ns=None, **extra):
    return ('pod', ns or NS, {'app': app, **extra})


def sel_match(selector, labels):
    for k, v in (selector.get('matchLabels') or {}).items():
        if labels.get(k) != v:
            return False
    for e in selector.get('matchExpressions') or []:
        has, val, op = e['key'] in labels, labels.get(e['key']), e['operator']
        if (op == 'In' and val not in e['values']) or (op == 'NotIn' and has and val in e['values']):
            return False
        if (op == 'Exists' and not has) or (op == 'DoesNotExist' and has):
            return False
    return True


def peer_match(peer, other):
    if not peer:  # 빈 peer = 모든 상대
        return True
    if other[0] == 'ip':
        block = peer.get('ipBlock')
        if not block:
            return False
        a = ipaddress.ip_address(other[1])
        return a in ipaddress.ip_network(block['cidr']) and not any(a in ipaddress.ip_network(n) for n in block.get('except', []))
    if 'ipBlock' in peer:
        return False
    ns_sel = peer.get('namespaceSelector')
    ns_ok = sel_match(ns_sel, other[1]) if ns_sel is not None else other[1] == NS  # namespaceSelector 없으면 정책과 같은 namespace
    pod_sel = peer.get('podSelector')
    return ns_ok and (sel_match(pod_sel, other[2]) if pod_sel is not None else True)


def port_match(rule, port, proto):
    ports = rule.get('ports')
    return not ports or any(p.get('port') == port and p.get('protocol', 'TCP') == proto for p in ports)  # ports 생략 = 모든 포트


def allowed(docs, direction, me, other, port, proto='TCP'):
    """me(ai-sre 안 파드 라벨)가 other 로 나가는가(Egress) / other 에게서 받는가(Ingress). 어떤 정책도 고르지 않으면 열려 있다."""
    key, side = ('egress', 'to') if direction == 'Egress' else ('ingress', 'from')
    isolated = False
    for p in docs:
        if p['kind'] != 'NetworkPolicy':
            continue
        spec = p['spec']
        if not sel_match(spec['podSelector'], me):
            continue
        if direction not in spec.get('policyTypes', ['Ingress'] + (['Egress'] if 'egress' in spec else [])):
            continue
        isolated = True
        for rule in spec.get(key) or []:
            if port_match(rule, port, proto) and (not rule.get(side) or any(peer_match(x, other) for x in rule[side])):
                return True
    return not isolated


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

    def eg(self, me, other, port, proto='TCP', docs=None):
        return allowed(docs or self.docs, 'Egress', {'app': me}, other, port, proto)

    def ing(self, me, other, port, docs=None):
        return allowed(docs or self.docs, 'Ingress', {'app': me}, other, port)

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

    def test_default_deny_is_empty_and_no_rule_opens_all_peers(self):
        deny = self.policies['default-deny']['spec']
        self.assertEqual((deny['podSelector'], sorted(deny['policyTypes'])), ({}, ['Egress', 'Ingress']))
        self.assertFalse(deny.get('ingress'))
        self.assertFalse(deny.get('egress'))
        for name, p in self.policies.items():
            for key, side in (('ingress', 'from'), ('egress', 'to')):
                for rule in p['spec'].get(key) or []:
                    self.assertTrue(rule.get(side) and all(rule[side]), f'{name}: 모든 상대를 여는 규칙')

    def test_evaluator_sees_an_opened_default_deny(self):
        # 시험이 정말 「열린 기본 거부」를 잡는지: 기본 거부에 빈 규칙을 넣으면 sandbox 가 10.0.0.1:22 로 나가게 되어야 한다
        opened = copy.deepcopy(self.docs)
        deny = next(d for d in opened if d['kind'] == 'NetworkPolicy' and d['metadata']['name'] == 'default-deny')
        deny['spec']['ingress'], deny['spec']['egress'] = [{}], [{}]
        self.assertTrue(self.eg('sandbox', ('ip', '10.0.0.1'), 22, docs=opened))
        self.assertTrue(self.ing('db', pod('sandbox'), 5432, docs=opened))
        self.assertFalse(self.eg('sandbox', ('ip', '10.0.0.1'), 22))

    def test_sandbox_reaches_only_anthropic_on_443_and_has_no_dns(self):
        ip = lambda a: ('ip', a)
        self.assertTrue(self.eg('sandbox', ip('160.79.104.10'), 443))
        self.assertTrue(self.eg('sandbox', ip('2607:6bc0::1'), 443))
        for other, port in [(ip('160.79.104.10'), 80), (ip('10.43.0.1'), 443), (ip('192.168.5.2'), 443), (ip('140.82.112.3'), 443),
                            (pod('db'), 5432), (pod('worker'), 8080), (pod('api'), 8080)]:
            self.assertFalse(self.eg('sandbox', other, port), (other, port))
        for proto in ('UDP', 'TCP'):  # DNS 로 데이터를 밖에 내보내는 길
            self.assertFalse(self.eg('sandbox', KUBE_DNS, 53, proto), proto)
        for any_other in (ip('10.43.0.10'), ip('8.8.8.8'), KUBE_DNS):
            self.assertFalse(self.eg('sandbox', any_other, 53, 'UDP'))
        cidrs = {peer['ipBlock']['cidr'] for r in self.policies['sandbox-egress']['spec']['egress'] for peer in r['to']}
        self.assertEqual(cidrs, ANTHROPIC)
        self.assertTrue(self.eg('worker', KUBE_DNS, 53, 'UDP'))  # 다른 파드의 DNS 는 그대로

    def test_sandbox_takes_only_worker_and_db_takes_only_its_clients(self):
        self.assertTrue(self.ing('sandbox', pod('worker'), 9999))
        for src in (pod('api'), pod('db'), pod('backup'), pod('other'), pod('worker', {'kubernetes.io/metadata.name': 'monitoring'})):
            self.assertFalse(self.ing('sandbox', src, 9999), src)
        for ok in ('api', 'worker', 'backup'):
            self.assertTrue(self.ing('db', pod(ok), 5432), ok)
        for bad in (pod('sandbox'), pod('other')):
            self.assertFalse(self.ing('db', bad, 5432), bad)
        self.assertFalse(self.ing('db', pod('api'), 5433))
        self.assertFalse(self.eg('db', pod('api'), 5432))  # db 는 나가는 길이 없다

    def test_api_takes_only_monitoring_alertmanager_and_prometheus(self):
        mon = {'kubernetes.io/metadata.name': 'monitoring'}
        for name in ('alertmanager', 'prometheus'):
            self.assertTrue(self.ing('api', ('pod', mon, {'app.kubernetes.io/name': name}), 8080), name)
        self.assertFalse(self.ing('api', ('pod', mon, {'app.kubernetes.io/name': 'grafana'}), 8080))
        self.assertFalse(self.ing('api', pod('worker'), 8080))
        self.assertFalse(self.ing('api', ('pod', mon, {'app.kubernetes.io/name': 'alertmanager'}), 9999))

    def test_worker_egress(self):
        mon = {'kubernetes.io/metadata.name': 'monitoring'}
        for port in (9090, 9093, 3100):
            self.assertTrue(self.eg('worker', ('pod', mon, {'app': 'x'}), port), port)
        self.assertFalse(self.eg('worker', ('pod', mon, {'app': 'x'}), 8080))
        self.assertTrue(self.eg('worker', pod('db'), 5432))
        self.assertTrue(self.eg('worker', pod('sandbox'), 1234))
        self.assertTrue(self.eg('worker', ('ip', '140.82.112.3'), 443))
        for a in ['10.43.0.1', '192.168.5.2', '172.16.0.1', '169.254.169.254', '100.64.0.1']:
            self.assertFalse(self.eg('worker', ('ip', a), 443), a)
        self.assertFalse(self.eg('worker', ('ip', '140.82.112.3'), 80))
        self.assertTrue(self.eg('backup', pod('db'), 5432))
        self.assertFalse(self.eg('backup', ('ip', '140.82.112.3'), 443))

    def test_unlabeled_pods_get_nothing_but_dns(self):
        self.assertFalse(self.eg('stranger', ('ip', '8.8.8.8'), 443))
        self.assertFalse(self.eg('stranger', pod('db'), 5432))
        self.assertFalse(self.ing('stranger', pod('worker'), 80))

    def test_pods_do_not_run_as_root_and_drop_capabilities(self):
        for kind in ('StatefulSet', 'CronJob'):
            d = next(x for x in self.docs if x['kind'] == kind)
            t = d['spec']['template'] if kind == 'StatefulSet' else d['spec']['jobTemplate']['spec']['template']
            self.assertEqual({k: t['spec']['securityContext'].get(k) for k in ('runAsNonRoot', 'runAsUser', 'fsGroup')}, {'runAsNonRoot': True, 'runAsUser': 70, 'fsGroup': 70})
            c = t['spec']['containers'][0]['securityContext']
            self.assertEqual((c['allowPrivilegeEscalation'], c['capabilities']), (False, {'drop': ['ALL']}))
            self.assertIs(t['spec']['automountServiceAccountToken'], False)

    def test_volumes_survive_deletion_and_backup_is_bounded(self):
        classes = [d['spec']['volumeClaimTemplates'][0]['spec']['storageClassName'] for d in self.docs if d['kind'] == 'StatefulSet']
        classes += [d['spec']['storageClassName'] for d in self.docs if d['kind'] == 'PersistentVolumeClaim']
        self.assertEqual(classes, ['local-path-retain'] * 2)
        cron = next(d for d in self.docs if d['kind'] == 'CronJob')
        job = cron['spec']['jobTemplate']['spec']
        self.assertEqual((cron['spec']['schedule'], cron['spec']['concurrencyPolicy']), ('20 4 * * *', 'Forbid'))
        self.assertEqual(job['template']['metadata']['labels'], {'app': 'backup'})
        self.assertEqual(job['activeDeadlineSeconds'], 1800)
        c = job['template']['spec']['containers'][0]
        script = c['command'][2]
        self.assertIn('PGCONNECT_TIMEOUT', {e['name'] for e in c['env']})
        # 순서: 옛 .part 치우기 → trap → 시간 제한 있는 dump → 끝까지 읽어 검증 → rename → 보존 정리
        order = ['rm -f /backups/*.part', "trap 'rm -f", '--lock-wait-timeout=', 'pg_restore -f /dev/null', 'mv "$f.part"', '-mtime +14 -delete']
        self.assertEqual([script.index(s) for s in order], sorted(script.index(s) for s in order))
        self.assertNotIn('--list', script)

    def test_backup_schedule_is_unique_among_apps(self):
        mine = next(d for d in self.docs if d['kind'] == 'CronJob')['spec']['schedule']
        for f in ROOT.glob('apps/*/*.yaml'):
            if f.parent.name != 'ai-sre':
                for line in f.read_text().splitlines():
                    if line.strip().startswith('schedule:'):
                        self.assertNotEqual(line.split('"')[1], mine, f'{f} 와 같은 분')


if __name__ == '__main__':
    unittest.main()
