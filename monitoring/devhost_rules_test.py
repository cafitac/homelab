# 개발 호스트 디스크 규칙(monitoring/rules.yaml 의 homelab.devhost)과 수집 설정(hostguard-scrape.yaml)을 클러스터 없이 검사한다.
# 실행: python3 -m unittest monitoring/devhost_rules_test.py   필요: yq · kubectl(kustomize 렌더만) · docker(promtool 은 아래 고정 이미지에서 돈다 — 내 promtool 을 쓰려면 PROMTOOL=<경로> 를 명시). CI: .github/workflows/devhost-rules.yml
# 확인하는 것: promtool check/test rules(뜰 때 · 안 뜰 때 — 자는 MacBook 은 2 시간 뒤 풀린다), 규칙이 쓰는 지표가 hostguard textfile 이 실제로 내는 것인지, 수집 대상 · 거르기.
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# 차트 kube-prometheus-stack 91.9.0 의 prometheusSpec.image(v3.15.0-distroless)와 같은 판(apps/ai-sre/monitoring_test.py 와 같다)
PROM_IMAGE = 'quay.io/prometheus/prometheus@sha256:b2a413d5a03ea6a76782a508d1c7947440bba3b973931a25676e278431891b01'
# agent-skills shared/scripts/hostguard/export.py 의 METRICS 가 내는 이름 전부(호스트 라벨 host)
EXPORTED = {'hostguard_free_bytes', 'hostguard_floor_bytes', 'hostguard_time_to_floor_active', 'hostguard_sample_timestamp_seconds'}
GIB = 1 << 30


def s(name, values, host, **extra):
    labels = ','.join(f'{k}="{v}"' for k, v in {'host': host, **extra}.items())
    return {'series': f'{name}{{{labels}}}', 'values': values}


def host_series(host, free_gib, ttf=0, ts='0+60x400'):
    """표본 시각 ts 는 시험 시각과 같이 오른다(1 분마다 갱신) — 자는 호스트는 중간에 멈춘 값으로 준다."""
    return [s('hostguard_free_bytes', f'{free_gib * GIB}x400', host), s('hostguard_floor_bytes', f'{10 * GIB}x400', host),
            s('hostguard_time_to_floor_active', f'{ttf}x400', host), s('hostguard_sample_timestamp_seconds', ts, host)]


def alert(name, at, *hosts):
    return {'eval_time': at, 'alertname': name, 'exp_alerts': [{'exp_labels': {'host': h}} for h in hosts]}


ASLEEP = '0+60x60 3540x400'  # 60 분째에 마지막 표본 — 3540 초 + 2 시간 = 179 분부터 오래된 값이다
CASES = [
    # 하한(10GiB) 아래이고 표본이 신선하면 for 15m 뒤에 뜬다 · 위이면 조용하다
    (host_series('macbook', 5), [alert('DevHostDiskLow', '10m'), alert('DevHostDiskLow', '20m', 'macbook')]),
    (host_series('macbook', 11) + host_series('macstudio', 50), [alert('DevHostDiskLow', '60m')]),
    (host_series('macbook', 10), [alert('DevHostDiskLow', '60m')]),  # 정확히 하한이면 조용하다 — hostguard BELOW_FLOOR 도 free < floor 다
    # 덮개를 닫은 MacBook — 마지막 표본이 2 시간 넘게 오래되면 같은 값이 남아 있어도 풀린다(경보가 아니다)
    (host_series('macbook', 5, ts=ASLEEP), [alert('DevHostDiskLow', '100m', 'macbook'), alert('DevHostDiskLow', '200m')]),
    # hostguard 가 계산한 TIME_TO_FLOOR 를 그대로 쓴다: 켜져 있으면 for 30m 뒤 · 꺼져 있으면 조용
    (host_series('macbook', 50, ttf=1), [alert('DevHostDiskFillingUp', '20m'), alert('DevHostDiskFillingUp', '40m', 'macbook')]),
    (host_series('macbook', 50, ttf=0), [alert('DevHostDiskFillingUp', '60m')]),
    # 자는 MacBook 은 TIME_TO_FLOOR=1 을 파일에 남긴 채여도 2 시간 뒤 풀린다(신선도 조건이 빠지면 여기서 걸린다)
    (host_series('macbook', 50, ttf=1, ts=ASLEEP), [alert('DevHostDiskFillingUp', '100m', 'macbook'), alert('DevHostDiskFillingUp', '200m')]),
    # textfile 을 다시 쓰는 찰나에 긁힌 한 번(스크레이프가 빈 파일을 봐 시계열이 2 분 stale)이 이미 뜬 경보를 풀었다 다시 띄우지 않는다
    ([s(n, v, 'macbook') for n, v in (('hostguard_free_bytes', f'{5 * GIB}x30 stale stale {5 * GIB}x400'), ('hostguard_floor_bytes', f'{10 * GIB}x400'),
                                      ('hostguard_sample_timestamp_seconds', '0+60x400'))],
     [alert('DevHostDiskLow', '29m', 'macbook'), alert('DevHostDiskLow', '32m', 'macbook'), alert('DevHostDiskLow', '40m', 'macbook')]),
    # Studio: 신선하면 조용 · 표본이 멈추면 179 분 뒤 + for 30m · 아예 없으면(내보내기 · 수집 끊김) for 30m 뒤. MacBook 의 부재는 경보가 아니다
    (host_series('macstudio', 50), [alert('DevHostDiskGuardStale', '300m')]),
    (host_series('macstudio', 50, ts=ASLEEP), [alert('DevHostDiskGuardStale', '200m'), alert('DevHostDiskGuardStale', '215m', 'macstudio')]),
    (host_series('macbook', 50), [alert('DevHostDiskGuardStale', '20m'), alert('DevHostDiskGuardStale', '35m', 'macstudio')]),
    # 이미 뜬 오래됨 경보가 긁기 한 번(빈 파일 → stale)을 지나도 같은 경보로 이어진다 — 실제 수집은 job · instance 라벨이 붙고 absent 쪽에는 없다
    ([s('hostguard_sample_timestamp_seconds', '0+60x60 3540x160 stale stale 3540x400', 'macstudio', job='hostguard-textfile', instance='host.lima.internal:9101')],
     [alert('DevHostDiskGuardStale', '215m', 'macstudio'), alert('DevHostDiskGuardStale', '222m', 'macstudio'), alert('DevHostDiskGuardStale', '230m', 'macstudio')]),
]


# 자식 프로세스 환경은 주는 것만 고정한다 — DOCKER_DEFAULT_PLATFORM · DOCKER_CONTEXT 같은 호출자 설정이 시험 결과를 바꾸지 않게 한다(DOCKER_HOST 는 colima 같은 전송 선택이라 남긴다)
ENV = {**{k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'DOCKER_HOST') if k in os.environ}, 'LC_ALL': 'C', 'TZ': 'UTC'}


def run(cmd, **kw):
    p = subprocess.run(cmd, text=True, capture_output=True, env=ENV, **kw)
    if p.returncode:
        raise AssertionError(f'{cmd[:4]} 실패({p.returncode}): {p.stdout[-3000:]}{p.stderr[-3000:]}')
    return p.stdout


def to_json(yaml_text):
    return [json.loads(line) for line in run(['yq', '-o=json', '-I=0', '.'], input=yaml_text).splitlines() if line.strip()]


class DevHostRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ('yq', 'kubectl'):
            if not shutil.which(tool):
                raise RuntimeError(f'필수 도구 없음: {tool}')
        # PATH 의 promtool 은 판이 제각각이라(keep_firing_for 는 2.42+) 쓰지 않는다 — 고정 이미지(CI 와 같다)가 기본이고, 내 것은 PROMTOOL 로 명시할 때만 쓴다
        override = os.environ.get('PROMTOOL')
        # 절대 경로로 바꿔 둔다 — promtool 은 임시 디렉터리를 cwd 로 돌므로 상대 경로(./tools/promtool)는 거기서 가리키는 곳이 달라진다
        cls.local_promtool = override and shutil.which(override) and os.path.abspath(shutil.which(override))
        if override and not cls.local_promtool:
            raise RuntimeError(f'PROMTOOL 을 실행할 수 없다: {override}')
        if not cls.local_promtool and not shutil.which('docker'):
            raise RuntimeError('docker 가 없다 — 고정 이미지로 promtool 을 돌릴 수 없다. 내 promtool 을 쓰려면 PROMTOOL=<경로> 를 명시한다(판이 달라 결과가 CI 와 다를 수 있다)')
        [rule] = to_json((ROOT / 'monitoring/rules.yaml').read_text())
        [group] = [g for g in rule['spec']['groups'] if g['name'] == 'homelab.devhost']
        cls.rules = {r['alert']: r for r in group['rules']}
        cls.tmp = Path(tempfile.mkdtemp())
        cls.tmp.chmod(0o755)  # promtool 이미지는 nobody 로 돈다
        (cls.tmp / 'rules.yaml').write_text(json.dumps({'groups': [group]}, ensure_ascii=False))
        logic = dict(group, rules=[{k: v for k, v in r.items() if k != 'annotations'} for r in group['rules']])  # 문구의 템플릿 문법은 check rules 가 본다
        (cls.tmp / 'logic.yaml').write_text(json.dumps({'groups': [logic]}))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def promtool(self, *args):
        if self.local_promtool:
            return run([self.local_promtool, *args], cwd=self.tmp)
        return run(['docker', 'run', '--rm', '--network', 'none', '-v', f'{self.tmp}:/w:ro', '-w', '/w', '--entrypoint', '/bin/promtool', PROM_IMAGE, *args])

    def test_promtool_check_rules(self):
        self.assertIn('SUCCESS', self.promtool('check', 'rules', 'rules.yaml'))
        self.assertEqual({n: r['labels']['severity'] for n, r in self.rules.items()},
                         {'DevHostDiskLow': 'critical', 'DevHostDiskFillingUp': 'warning', 'DevHostDiskGuardStale': 'warning'})
        self.assertEqual({n for n, r in self.rules.items() if r.get('keep_firing_for')}, {'DevHostDiskLow', 'DevHostDiskFillingUp'})

    def test_promtool_unit_tests(self):
        tests = []
        for series, checks in CASES:
            for c in checks:
                for a in c['exp_alerts']:
                    a['exp_labels']['severity'] = self.rules[c['alertname']]['labels']['severity']
            tests.append({'interval': '1m', 'input_series': series, 'alert_rule_test': checks})
        (self.tmp / 'tests.yaml').write_text(json.dumps({'rule_files': ['logic.yaml'], 'evaluation_interval': '1m', 'tests': tests}))
        self.assertIn('SUCCESS', self.promtool('test', 'rules', 'tests.yaml'))

    def test_freshness_edge_is_exactly_7200_seconds(self):
        # 규칙 식 그대로를 경계에서 직접 본다: ASLEEP 의 표본 시각은 3540 이라 178 분에 나이 7140 초 · 179 분에 정확히 7200 초 · 180 분에 7260 초다
        # 신선함은 < 7200 이라 179 분에 사라지고, 오래됨은 > 7200 이라 179 분에도 아직 아니다 — 부등호가 <= · >= 로 바뀌면 179 분이 달라진다
        def expr_test(name, at, *samples):
            return {'expr': self.rules[name]['expr'], 'eval_time': at,
                    'exp_samples': [{'labels': lbl, 'value': v} for lbl, v in samples]}
        low = 'hostguard_free_bytes{host="macbook"}'
        up = 'hostguard_time_to_floor_active{host="macbook"}'
        stale = '{host="macstudio"}'
        tests = [
            {'interval': '1m', 'input_series': host_series('macbook', 5, ttf=1, ts=ASLEEP), 'promql_expr_test': [
                expr_test('DevHostDiskLow', '178m', (low, 5 * GIB)), expr_test('DevHostDiskLow', '179m'),
                expr_test('DevHostDiskFillingUp', '178m', (up, 1)), expr_test('DevHostDiskFillingUp', '179m')]},
            {'interval': '1m', 'input_series': host_series('macstudio', 50, ts=ASLEEP), 'promql_expr_test': [
                expr_test('DevHostDiskGuardStale', '178m'), expr_test('DevHostDiskGuardStale', '179m'),
                expr_test('DevHostDiskGuardStale', '180m', (stale, 7260))]},
        ]
        (self.tmp / 'edge.yaml').write_text(json.dumps({'rule_files': ['logic.yaml'], 'evaluation_interval': '1m', 'tests': tests}))
        self.assertIn('SUCCESS', self.promtool('test', 'rules', 'edge.yaml'))

    def test_rules_use_only_exported_metrics(self):
        used = set()
        for r in self.rules.values():
            used |= set(re.findall(r'\bhostguard_[a-z0-9_]+', r['expr']))
        self.assertEqual(used, EXPORTED)

    def test_scrape_config_is_selected_by_prometheus_and_keeps_only_hostguard_metrics(self):
        out = run(['kubectl', 'kustomize', 'monitoring'], cwd=ROOT)
        [scrape] = [d for d in to_json(out) if d['kind'] == 'ScrapeConfig']
        self.assertEqual(scrape['metadata']['labels'], {'release': 'kps'})  # 라이브 Prometheus 의 scrapeConfigSelector(2026-11-16)
        self.assertEqual((scrape['spec']['jobName'], scrape['spec']['staticConfigs']), ('hostguard-textfile', [{'targets': ['host.lima.internal:9101']}]))
        [relabel] = scrape['spec']['metricRelabelings']
        keep = re.compile(relabel['regex'])  # Prometheus 의 정규식은 전체 일치다
        self.assertEqual(relabel['action'], 'keep')
        self.assertTrue(all(keep.fullmatch(n) for n in EXPORTED | {'node_textfile_mtime_seconds', 'node_textfile_scrape_error'}))
        self.assertFalse(any(keep.fullmatch(n) for n in ('go_goroutines', 'process_cpu_seconds_total', 'node_exporter_build_info')))


if __name__ == '__main__':
    unittest.main()
