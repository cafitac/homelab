# 렌더된 매니페스트가 격리 약속을 지키는지 본다(클러스터에 닿지 않는다). 실행: python3 -m unittest apps/ai-sre/manifest_test.py
# NetworkPolicy 는 쿠버네티스 의미 그대로 모형화한다: 정책은 합산(더하기만), ports 생략 · port 없는 항목 = 모든 포트, to/from 생략 · 빈 peer = 모든 상대.
import copy
import os
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
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
    if not ports:  # ports 생략 = 모든 포트
        return True
    for p in ports:  # port 없는 항목({} · {protocol: TCP}) = 그 프로토콜의 모든 포트, protocol 생략 = TCP
        if p.get('protocol', 'TCP') != proto:
            continue
        if 'port' not in p or p['port'] == port or (p.get('endPort') and p['port'] <= port <= p['endPort']):
            return True
    return False


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
                    if 'ports' in rule:
                        self.assertTrue(rule['ports'] and all('port' in x for x in rule['ports']), f'{name}: 모든 포트를 여는 항목')

    def test_evaluator_sees_an_opened_default_deny(self):
        # 시험이 정말 「열린 기본 거부」를 잡는지: 기본 거부에 빈 규칙을 넣으면 sandbox 가 10.0.0.1:22 로 나가게 되어야 한다
        opened = copy.deepcopy(self.docs)
        deny = next(d for d in opened if d['kind'] == 'NetworkPolicy' and d['metadata']['name'] == 'default-deny')
        deny['spec']['ingress'], deny['spec']['egress'] = [{}], [{}]
        self.assertTrue(self.eg('sandbox', ('ip', '10.0.0.1'), 22, docs=opened))
        self.assertTrue(self.ing('db', pod('sandbox'), 5432, docs=opened))
        self.assertFalse(self.eg('sandbox', ('ip', '10.0.0.1'), 22))

    def test_evaluator_sees_wildcard_port_entries(self):
        # sandbox 에 「아무 곳 · 모든 포트」 정책이 더해지면(합산) 새어 나가야 한다 — port 없는 항목은 모든 포트다
        for ports in ([{}], [{'protocol': 'TCP'}]):
            opened = copy.deepcopy(self.docs)
            sb = next(d for d in opened if d['kind'] == 'NetworkPolicy' and d['metadata']['name'] == 'sandbox-egress')
            sb['spec']['egress'] = [{'to': [{'ipBlock': {'cidr': '0.0.0.0/0'}}], 'ports': ports}]
            self.assertTrue(self.eg('sandbox', ('ip', '10.0.0.1'), 22, docs=opened), ports)
            self.assertTrue(self.eg('sandbox', ('ip', '8.8.8.8'), 443, docs=opened), ports)
        udp = copy.deepcopy(self.docs)
        sb = next(d for d in udp if d['kind'] == 'NetworkPolicy' and d['metadata']['name'] == 'sandbox-egress')
        sb['spec']['egress'] = [{'to': [{'ipBlock': {'cidr': '0.0.0.0/0'}}], 'ports': [{'protocol': 'UDP'}]}]
        self.assertTrue(self.eg('sandbox', ('ip', '8.8.8.8'), 53, 'UDP', docs=udp))
        self.assertFalse(self.eg('sandbox', ('ip', '8.8.8.8'), 443, docs=udp))
        self.assertFalse(self.eg('sandbox', ('ip', '8.8.8.8'), 443))

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
        order = ['rm -f /backups/*.part', "trap 'rm -f", '\nprune\n', '--lock-wait-timeout=', 'pg_restore -f /dev/null', 'mv "$f.part"']
        pos = [script.index(x) for x in order]
        self.assertEqual(pos, sorted(pos))
        self.assertEqual(script.count('\nprune\n'), 2)  # 덤프 전 · 뒤
        self.assertNotIn('--list', script)

    # --- 백업 스크립트 실행: 가짜 pg_dump(용량 한도가 있어 차면 ENOSPC) · pg_restore 로 렌더된 스크립트를 임시 디렉터리에서 돌린다 ---
    FAKE_PG_DUMP = '''#!/bin/sh
for a; do out=$a; done
[ -z "$FAKE_FAIL" ] || { echo "pg_dump: error: connection failed" >&2; exit 1; }
used=$(cat "$BK"/ai-sre-*.dump "$BK"/*.part 2>/dev/null | wc -c)
[ $((used + 100)) -le "$CAP" ] || { echo "pg_dump: error: could not write: No space left on device" >&2; exit 1; }
head -c 100 /dev/zero > "$out"
'''

    def run_backup(self, files, cap, fail=False, drop_prune=False):
        """files: {이름: (바이트, 며칠 전)}. 가짜 도구로 렌더된 백업 스크립트를 돌려 (종료코드, 남은 파일) 를 돌려준다."""
        cron = next(d for d in self.docs if d['kind'] == 'CronJob')
        script = cron['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['command'][2]
        if drop_prune:
            script = script.replace('\nprune\n', '\n:\n', 1)  # 덤프 전 정리만 뺀다
        with tempfile.TemporaryDirectory() as tmp:
            bk, bin_ = Path(tmp, 'bk'), Path(tmp, 'bin')
            bk.mkdir(), bin_.mkdir()
            for name, (size, days) in files.items():
                f = bk / name
                f.write_bytes(b'x' * size)
                t = time.time() - days * 86400
                os.utime(f, (t, t))
            (bin_ / 'pg_dump').write_text(self.FAKE_PG_DUMP)
            (bin_ / 'pg_restore').write_text('#!/bin/sh\nexit 0\n')
            for tool in bin_.iterdir():
                tool.chmod(0o755)
            env = {**os.environ, 'PATH': f'{bin_}:{os.environ["PATH"]}', 'BK': str(bk), 'CAP': str(cap), **({'FAKE_FAIL': '1'} if fail else {})}
            r = subprocess.run(['sh', '-c', script.replace('/backups', str(bk))], env=env, text=True, capture_output=True)
            return r.returncode, sorted(p.name for p in bk.iterdir())

    def test_full_disk_recovers_by_pruning_before_dump(self):
        # 디스크가 찼다(합 1000 = 한도). 30 일 · 20 일 된 완성본이 자리를 차지한다.
        full = {'ai-sre-old30.dump': (450, 30), 'ai-sre-old20.dump': (450, 20), 'ai-sre-fresh.dump': (100, 1)}
        rc, left = self.run_backup(full, cap=1000)
        self.assertEqual(rc, 0)
        self.assertIn('ai-sre-fresh.dump', left)
        self.assertEqual([n for n in left if 'old' in n], [])  # 기간 지난 것은 덤프 전에 회수됐다
        self.assertEqual(len(left), 2)  # fresh + 이번 새 덤프
        # 덤프 전 정리가 없으면 같은 상황에서 ENOSPC 로 실패한다(회귀 시험)
        rc, left = self.run_backup(full, cap=1000, drop_prune=True)
        self.assertNotEqual(rc, 0)

    def test_newest_backup_is_kept_even_when_older_than_the_window_and_dump_fails(self):
        files = {'ai-sre-old30.dump': (450, 30), 'ai-sre-old20.dump': (450, 20)}
        rc, left = self.run_backup(files, cap=1000, fail=True)
        self.assertNotEqual(rc, 0)
        self.assertEqual(left, ['ai-sre-old20.dump'])  # 가장 최근 완성본은 남고, 더 오래된 것만 회수
        # 다음 실행(디스크 여유가 생김)은 성공하고, 새 백업이 생기면 기간 지난 옛것은 정리된다
        rc, left = self.run_backup({'ai-sre-old20.dump': (450, 20)}, cap=1000)
        self.assertEqual((rc, len(left)), (0, 1))
        self.assertNotIn('ai-sre-old20.dump', left)

    def test_only_backup_survives_age_and_stale_part_is_removed(self):
        rc, left = self.run_backup({'ai-sre-only.dump': (10, 90), 'ai-sre-x.dump.part': (900, 0)}, cap=1000, fail=True)
        self.assertEqual((rc, left), (1, ['ai-sre-only.dump']))

    def test_backup_schedule_is_unique_among_apps(self):
        mine = next(d for d in self.docs if d['kind'] == 'CronJob')['spec']['schedule']
        for f in ROOT.glob('apps/*/*.yaml'):
            if f.parent.name != 'ai-sre':
                for line in f.read_text().splitlines():
                    if line.strip().startswith('schedule:'):
                        self.assertNotEqual(line.split('"')[1], mine, f'{f} 와 같은 분')


if __name__ == '__main__':
    unittest.main()
