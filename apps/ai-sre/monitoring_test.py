# ai-sre 자기 관측(monitoring.yaml)을 클러스터 없이 검사한다. 실행: python3 -m unittest apps/ai-sre/monitoring_test.py
# 필요: kubectl(kustomize 렌더만) · yq · docker(promtool 이 PATH 에 있으면 그것을 쓰고, 없으면 아래 고정 이미지). CI: .github/workflows/ai-sre-monitoring.yml
# 확인하는 것: promtool check/test rules(경보가 뜨는 때 · 안 뜨는 때), 규칙이 쓰는 지표 이름이 ai-sre 가 실제로 내는 것인지, PodMonitor 가 고르는 포트.
import json
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
# 차트 kube-prometheus-stack 91.9.0 의 prometheusSpec.image(v3.15.0-distroless)와 같은 판
PROM_IMAGE = 'quay.io/prometheus/prometheus@sha256:b2a413d5a03ea6a76782a508d1c7947440bba3b973931a25676e278431891b01'
# cafitac/ai-sre c9ea73a 가 /metrics 로 내는 이름 전부(src/ai_sre/metrics/{process,pipeline,endpoint}.py) — api 와 worker 를 합친 것.
# ai_sre_secret_scan_drops_total 은 diagnosis/fanout.py 에 정의만 있고 worker /metrics 에 연결되지 않아 여기 없다(연결되면 더한다)
EXPORTED = {
    'ai_sre_http_requests_total', 'ai_sre_pipeline_collect_success', 'ai_sre_jobs', 'ai_sre_job_last_done_timestamp_seconds',
    'ai_sre_job_oldest_due_age_seconds', 'ai_sre_job_expired_leases', 'ai_sre_outbox_messages', 'ai_sre_outbox_oldest_pending_age_seconds',
    'ai_sre_diagnosis_oldest_running_age_seconds', 'ai_sre_ledger_last_observed_timestamp_seconds',
    'ai_sre_worker_last_tick_timestamp_seconds', 'ai_sre_worker_ticks_total',
}
NS = 'ai-sre'


def s(name, values, **labels):
    labels = {'namespace': NS, **labels}
    return {'series': name + '{' + ','.join(f'{k}="{v}"' for k, v in labels.items()) + '}', 'values': values}


def alert(name, at, *labels):
    return {'eval_time': at, 'alertname': name, 'exp_alerts': [{'exp_labels': {'namespace': NS, **x}} for x in labels]}


API, WORKER = {'job': 'api', 'pod': 'api-1'}, {'job': 'worker', 'pod': 'worker-1'}
# (입력 시계열, 기대). 기대의 라벨에 severity 는 아래에서 규칙 값으로 채운다. 시간 단위 1m
CASES = [
    # api 는 떠 있고 worker 는 수집 대상이 아예 없다(absent) / api 가 응답하지 않는다(up 0)
    ([s('up', '1x20', **API)], [alert('AiSreTargetDown', '4m'), alert('AiSreTargetDown', '6m', {'job': 'worker'})]),
    ([s('up', '0x20', **API), s('up', '1x20', **WORKER)], [alert('AiSreTargetDown', '6m', API)]),
    # tick 이 1분마다 끝난다 · 300 초짜리 진단 하나가 끼어도 조용하다
    ([s('up', '1x40', **WORKER), s('ai_sre_worker_last_tick_timestamp_seconds', '0+60x10 600x5 960+60x25', **WORKER)],
     [alert('AiSreWorkerHeartbeatStale', '40m')]),
    # 마지막 tick 이 1분에 끝나고 멈췄다 → 6분에 300 초를 넘고 for 10m 뒤(16분)에 뜬다
    ([s('up', '1x40', **WORKER), s('ai_sre_worker_last_tick_timestamp_seconds', '60x40', **WORKER)],
     [alert('AiSreWorkerHeartbeatStale', '15m'), alert('AiSreWorkerHeartbeatStale', '17m', WORKER)]),
    # worker 는 떠 있는데 tick 이 한 번도 끝나지 않았다(DB 에 못 붙음)
    ([s('up', '1x40', **WORKER)], [alert('AiSreWorkerHeartbeatStale', '9m'), alert('AiSreWorkerHeartbeatStale', '11m', WORKER)]),
    # worker 가 죽었다 — 하트비트 경보는 겹쳐 뜨지 않는다(AiSreTargetDown 만)
    ([s('up', '0x40', **WORKER), s('ai_sre_worker_last_tick_timestamp_seconds', '60x40', **WORKER)], [alert('AiSreWorkerHeartbeatStale', '30m')]),
    # 원장: 한 번도 관측하지 않은 대상(0, DEPLOY_OBSERVE 연결 전)은 뜨지 않는다 · 60 초마다 오르면 조용하다 · 1분에 멈추면 21분 뒤 뜬다
    ([s('ai_sre_ledger_last_observed_timestamp_seconds', '0x60', target='interview-coach', **API),
      s('ai_sre_ledger_last_observed_timestamp_seconds', '0+60x60', target='judge-board', **API)], [alert('AiSreLedgerStale', '60m')]),
    ([s('ai_sre_ledger_last_observed_timestamp_seconds', '60x60', target='judge-board', **API)],
     [alert('AiSreLedgerStale', '20m'), alert('AiSreLedgerStale', '22m', {'target': 'judge-board', **API})]),
    # 진단 하나가 RUNNING 으로 남았다: 31분에 1800 초를 넘고 for 5m
    ([s('ai_sre_diagnosis_oldest_running_age_seconds', '0+60x45', **API)],
     [alert('AiSreDiagnosisStuck', '35m'), alert('AiSreDiagnosisStuck', '37m', API)]),
    # DEAD 가 31분에 하나 늘었다 — api 가 그 사이 다시 떠서(pod 이름이 바뀜) 시계열이 갈렸어도 잡고, 30분 뒤에는 해소된다
    ([s('ai_sre_outbox_messages', '2x20', channel='DISCORD', status='DEAD', job='api', pod='api-a'),
      s('ai_sre_outbox_messages', '_x20 2x10 3x40', channel='DISCORD', status='DEAD', job='api', pod='api-b')],
     [alert('AiSreOutboxDead', '25m'), alert('AiSreOutboxDead', '35m', {'channel': 'DISCORD'}), alert('AiSreOutboxDead', '65m')]),
    # 웹훅 401(4xx) 이 1분마다 · 2xx 는 아무리 많아도 조용하다
    ([s('ai_sre_http_requests_total', '0+1x20', route='/v1/alertmanager/webhook', method='POST', code='4xx', **API),
      s('ai_sre_http_requests_total', '0+50x20', route='/v1/alertmanager/webhook', method='POST', code='2xx', **API)],
     [alert('AiSreWebhookErrors', '10m', {'code': '4xx'})]),
    ([s('ai_sre_http_requests_total', '0+50x20', route='/v1/alertmanager/webhook', method='POST', code='2xx', **API),
      s('ai_sre_http_requests_total', '0+1x20', route='/healthz', method='GET', code='5xx', **API)], [alert('AiSreWebhookErrors', '10m')]),
    ([s('ai_sre_pipeline_collect_success', '1x5 0x20', **API)],
     [alert('AiSrePipelineMetricsUnavailable', '14m'), alert('AiSrePipelineMetricsUnavailable', '17m', API)]),
]


def run(cmd, **kw):
    p = subprocess.run(cmd, text=True, capture_output=True, **kw)
    if p.returncode:
        raise AssertionError(f'{cmd[:4]} 실패({p.returncode}): {p.stdout[-3000:]}{p.stderr[-3000:]}')
    return p.stdout


class MonitoringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ('kubectl', 'yq'):
            if not shutil.which(tool):
                raise RuntimeError(f'필수 도구 없음: {tool}')
        cls.local_promtool = shutil.which('promtool')
        if not cls.local_promtool and not shutil.which('docker'):
            raise RuntimeError('필수 도구 없음: promtool 또는 docker')
        out = run(['kubectl', 'kustomize', 'apps/ai-sre'], cwd=ROOT)
        cls.docs = [json.loads(line) for line in run(['yq', '-o=json', '-I=0', '.'], input=out).splitlines() if line.strip()]
        [cls.rule] = [d for d in cls.docs if d['kind'] == 'PrometheusRule']
        [cls.monitor] = [d for d in cls.docs if d['kind'] == 'PodMonitor']
        cls.rules = {r['alert']: r for g in cls.rule['spec']['groups'] for r in g['rules']}
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / 'rules.yaml').write_text(json.dumps({'groups': cls.rule['spec']['groups']}, ensure_ascii=False))
        # 단위 시험은 뜨는 때 · 라벨만 본다 — 문구(annotations)를 뺀 사본. 문구의 템플릿 문법은 check rules 가 본다
        logic = [dict(g, rules=[{k: v for k, v in r.items() if k != 'annotations'} for r in g['rules']]) for g in cls.rule['spec']['groups']]
        (cls.tmp / 'logic.yaml').write_text(json.dumps({'groups': logic}))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def promtool(self, *args):
        if self.local_promtool:
            return run([self.local_promtool, *args], cwd=self.tmp)
        return run(['docker', 'run', '--rm', '--network', 'none', '-v', f'{self.tmp}:/w:ro', '-w', '/w', '--entrypoint', '/bin/promtool', PROM_IMAGE, *args])

    def test_promtool_check_rules(self):
        self.assertIn('SUCCESS', self.promtool('check', 'rules', 'rules.yaml'))

    def test_promtool_unit_tests(self):
        tests = []
        for series, checks in CASES:
            for c in checks:
                for a in c['exp_alerts']:
                    a['exp_labels']['severity'] = self.rules[c['alertname']]['labels']['severity']
            tests.append({'interval': '1m', 'input_series': series, 'alert_rule_test': checks})
        (self.tmp / 'tests.yaml').write_text(json.dumps({'rule_files': ['logic.yaml'], 'evaluation_interval': '1m', 'tests': tests}))
        self.assertIn('SUCCESS', self.promtool('test', 'rules', 'tests.yaml'))

    def test_rules_use_only_exported_metrics(self):
        used = set()
        for r in self.rules.values():
            used |= set(re.findall(r'\bai_sre_[a-z0-9_]+', r['expr']))
        self.assertTrue(used)
        self.assertEqual(used - EXPORTED, set())

    def test_every_alert_reaches_the_discord_route(self):
        # monitoring/kube-prometheus-stack.values.yaml 맨 끝 경로: alertname =~ "AiSre.*", severity critical|warning
        self.assertGreaterEqual(set(self.rules), {'AiSreWorkerHeartbeatStale', 'AiSreLedgerStale', 'AiSreTargetDown'})
        for name, r in self.rules.items():
            self.assertTrue(name.startswith('AiSre'), name)
            self.assertIn(r['labels']['severity'], ('critical', 'warning'), name)
            self.assertTrue(r['annotations']['summary'], name)
        self.assertEqual(self.rule['metadata']['namespace'], NS)

    def test_pod_monitor_scrapes_api_and_worker_by_port_name(self):
        spec = self.monitor['spec']
        self.assertEqual(self.monitor['metadata']['namespace'], NS)
        self.assertEqual(spec['jobLabel'], 'app')  # job 라벨 = api · worker (규칙이 job 으로 고른다)
        self.assertNotIn('namespaceSelector', spec)  # 자기 namespace 만
        self.assertEqual(spec['selector'], {'matchExpressions': [{'key': 'app', 'operator': 'In', 'values': ['api', 'worker']}]})
        ports = {}
        for d in self.docs:
            if d['kind'] == 'Deployment' and d['metadata']['name'] in ('api', 'worker'):
                c = d['spec']['template']['spec']['containers'][0]
                ports[d['metadata']['name']] = {p['name']: p['containerPort'] for p in c['ports']}
        self.assertEqual(ports, {'api': {'http': 8080}, 'worker': {'metrics': 8080}})
        self.assertEqual([(e['port'], e['path']) for e in spec['podMetricsEndpoints']], [('http', '/metrics'), ('metrics', '/metrics')])


if __name__ == '__main__':
    unittest.main()
