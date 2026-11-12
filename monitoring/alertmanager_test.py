# Alertmanager 설정을 차트로 렌더해 검사한다(클러스터에 닿지 않는다). 실행: python3 -m unittest monitoring/alertmanager_test.py
# 필요: helm · yq · docker(amtool 이 PATH 에 있으면 그것을 쓰고, 없으면 아래 고정 이미지). CI: .github/workflows/amtool.yml
# 핵심 약속: ai-sre 경로는 맨 앞 + continue 라 Discord 로 가는 경보는 ai-sre 경로가 없을 때와 똑같다.
# B9b: ai-sre 자신의 경보(AiSre.*)는 맨 끝 Discord 경로 하나로만 간다 — 그 밖의 경보 수신처 · Alertmanager CR 은 그대로다.
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# 차트 91.9.0 의 alertmanagerSpec.image 와 같은 판(v0.34.1)
AM_IMAGE = 'quay.io/prometheus/alertmanager@sha256:e9733bafb1bdef9b00e25a21f8f99dc26a22224bf16641ad754d1649f4c3357a'
TOKEN_FILE = '/etc/alertmanager/secrets/alertmanager-discord/ai-sre-token'  # 이미 마운트된 Secret 의 키 하나

# (라벨, ai-sre 경로를 더한 뒤 받는 곳). Discord 쪽 기대값은 ai-sre 경로가 없던 때(homelab 28de702)와 같다
CASES = [
    ({'alertname': 'InterviewCoachServerDown', 'severity': 'critical', 'namespace': 'interview-coach'}, ['ai-sre', 'discord']),
    ({'alertname': 'KubePodCrashLooping', 'severity': 'warning', 'namespace': 'judge-board'}, ['ai-sre', 'discord']),
    ({'alertname': 'PublicSiteDown', 'severity': 'critical', 'instance': 'https://gather.cafitac.com'}, ['ai-sre', 'discord']),
    ({'alertname': 'NodeDataDiskFillingUp', 'severity': 'warning'}, ['ai-sre', 'discord']),
    ({'alertname': 'KubePodCrashLooping', 'severity': 'warning', 'namespace': 'kube-system'}, ['ai-sre']),
    ({'alertname': 'KubePodNotReady', 'severity': 'info', 'namespace': 'interview-coach'}, ['null']),
    ({'alertname': 'Watchdog', 'severity': 'none'}, ['null']),
    ({'alertname': 'InfoInhibitor', 'severity': 'none', 'namespace': 'judge-board'}, ['null']),
    # B9b — ai-sre 자신의 경보는 Discord 로만(ai-sre 웹훅으로는 돌아가지 않는다). info 이하는 여전히 버린다
    ({'alertname': 'AiSreWebhookErrors', 'severity': 'warning', 'namespace': 'ai-sre'}, ['discord']),
    ({'alertname': 'AiSreWorkerHeartbeatStale', 'severity': 'critical', 'namespace': 'ai-sre'}, ['discord']),
    ({'alertname': 'AiSreTargetDown', 'severity': 'critical', 'namespace': 'ai-sre', 'job': 'worker'}, ['discord']),
    ({'alertname': 'AiSreLedgerStale', 'severity': 'warning', 'namespace': 'ai-sre', 'target': 'judge-board'}, ['discord']),
    ({'alertname': 'AiSreLedgerStale', 'severity': 'info', 'namespace': 'ai-sre'}, ['null']),
    ({'alertname': 'KubePodCrashLooping', 'severity': 'warning', 'namespace': 'ai-sre'}, ['null']),
    ({'alertname': 'AlertmanagerFailedToSendAlerts', 'severity': 'warning', 'namespace': 'monitoring', 'integration': 'webhook'}, ['null']),
]
# ai-sre 경로 · 수신처를 뺀 설정(경로 · Discord 수신처 · 묶음 · 억제 · 템플릿)의 지문 — main 28de702 를 렌더해 얻은 값이다.
# Discord 쪽을 일부러 바꿀 때만 고친다(검사 대상과 독립된 기준). B9b 의 AiSre 경로도 빼고 계산하므로 값은 그대로다
BASELINE_SHA256 = '8dd2b917a9d205cb26704607dcf66d5d75b30796e4009b6bcc8842d0eaae4f4e'
# 렌더된 Alertmanager CR 전체의 지문(main 361050d). 같으면 오퍼레이터가 파드를 다시 만들 일이 없다 — 설정 Secret 만 바뀐다
ALERTMANAGER_CR_SHA256 = '7ceb9570d388931f2e1c40d89b51e6e43b64958858b5138bf91cb0a8bec20bf3'
AI_SRE_OWN_ROUTE = {'receiver': 'discord', 'matchers': ['severity =~ "critical|warning"', 'alertname =~ "AiSre.*"']}


def is_ai_sre_own(labels):
    return labels['alertname'].startswith('AiSre')


def without_ai_sre(config):
    keep = [r for r in config['route']['routes'] if r['receiver'] != 'ai-sre' and r != AI_SRE_OWN_ROUTE]
    return dict(config, route=dict(config['route'], routes=keep), receivers=[r for r in config['receivers'] if r['name'] != 'ai-sre'])


def fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def run(cmd, **kw):
    p = subprocess.run(cmd, text=True, capture_output=True, **kw)
    if p.returncode:
        raise AssertionError(f'{cmd[:4]} 실패({p.returncode}): {p.stderr[-2000:]}')
    return p.stdout


def to_json(yaml_text, expr='.'):
    return [json.loads(line) for line in run(['yq', '-o=json', '-I=0', expr], input=yaml_text).splitlines() if line.strip() and line != 'null']


class AlertmanagerConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ('helm', 'yq'):
            if not shutil.which(tool):
                raise RuntimeError(f'필수 도구 없음: {tool}')
        cls.local_amtool = shutil.which('amtool')
        if not cls.local_amtool and not shutil.which('docker'):
            raise RuntimeError('필수 도구 없음: amtool 또는 docker')
        cls.tmp = Path(tempfile.mkdtemp())
        app = to_json((ROOT / 'argocd/apps/monitoring.yaml').read_text())[0]
        kps = next(s for s in app['spec']['sources'] if s.get('chart') == 'kube-prometheus-stack')
        env = {**os.environ, **{k: str(cls.tmp / k) for k in ('HELM_CACHE_HOME', 'HELM_CONFIG_HOME', 'HELM_DATA_HOME')}}
        run(['helm', 'pull', '--repo', kps['repoURL'], kps['chart'], '--version', kps['targetRevision'], '--untar', '-d', str(cls.tmp / 'chart')], env=env)
        out = run(['helm', 'template', 'kps', str(cls.tmp / 'chart' / kps['chart']), '-n', 'monitoring',
                   '-f', str(ROOT / 'monitoring/kube-prometheus-stack.values.yaml')], env=env)
        docs = to_json(out)
        secret = next(d for d in docs if d['kind'] == 'Secret' and 'alertmanager.yaml' in d.get('data', {}))
        cls.config_text = base64.b64decode(secret['data']['alertmanager.yaml']).decode()
        cls.config = to_json(cls.config_text)[0]
        cls.alertmanager = next(d for d in docs if d['kind'] == 'Alertmanager')
        cls.spec = cls.alertmanager['spec']
        (cls.tmp / 'cfg').mkdir()
        (cls.tmp / 'cfg/full.yaml').write_text(cls.config_text)
        (cls.tmp / 'cfg/without-ai-sre.yaml').write_text(json.dumps(without_ai_sre(cls.config), ensure_ascii=False))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def amtool(self, *args):
        if self.local_amtool:
            return run([self.local_amtool, *[a.replace('/cfg/', f'{self.tmp}/cfg/') for a in args]])
        return run(['docker', 'run', '--rm', '-v', f'{self.tmp}/cfg:/cfg:ro', '--entrypoint', 'amtool', AM_IMAGE, *args])

    def receivers(self, config, labels):
        out = self.amtool('config', 'routes', 'test', f'--config.file=/cfg/{config}', *[f'{k}={v}' for k, v in labels.items()])
        return sorted(out.strip().split(','))

    def test_amtool_check_config(self):
        self.assertIn('SUCCESS', self.amtool('check-config', '/cfg/full.yaml'))

    def test_ai_sre_route_is_first_and_continues(self):
        first = self.config['route']['routes'][0]
        self.assertEqual(first['receiver'], 'ai-sre')
        self.assertIs(first['continue'], True)
        self.assertEqual(self.config['route']['receiver'], 'null')  # 어디에도 안 맞는 경보는 여전히 버린다
        for r in self.config['route']['routes'][1:]:
            self.assertNotEqual(r['receiver'], 'ai-sre')

    def test_ai_sre_receiver(self):
        receiver = next(r for r in self.config['receivers'] if r['name'] == 'ai-sre')
        self.assertEqual(list(receiver), ['name', 'webhook_configs'])
        (hook,) = receiver['webhook_configs']
        self.assertEqual(hook['url'], 'http://api.ai-sre.svc:8080/v1/alertmanager/webhook')
        self.assertEqual((hook['max_alerts'], hook['send_resolved']), (50, True))
        self.assertEqual(hook['http_config'], {'authorization': {'type': 'Bearer', 'credentials_file': TOKEN_FILE}})

    def test_pod_template_is_untouched_so_alertmanager_does_not_restart(self):
        # 파드가 다시 뜨면 emptyDir 의 silence · 알림 기록이 사라진다 — 토큰은 이미 마운트된 Secret 의 키로만 받는다
        self.assertEqual(self.spec['secrets'], ['alertmanager-discord'])
        for key in ('volumes', 'volumeMounts', 'configMaps', 'storage', 'podMetadata'):
            self.assertNotIn(key, self.spec, key)

    def test_alertmanager_cr_is_unchanged(self):
        self.assertEqual(fingerprint(self.alertmanager), ALERTMANAGER_CR_SHA256)

    def test_everything_but_ai_sre_matches_the_frozen_baseline(self):
        self.assertEqual(fingerprint(without_ai_sre(self.config)), BASELINE_SHA256)

    def test_ai_sre_own_route_is_present_once(self):
        # without_ai_sre 가 빼는 것은 정확히 이 경로 하나다 — 모양이 바뀌면 지문 검사가 무엇을 빼는지 모르게 된다
        self.assertEqual([r for r in self.config['route']['routes'] if r == AI_SRE_OWN_ROUTE], [AI_SRE_OWN_ROUTE])

    def test_routing_table(self):
        for labels, expected in CASES:
            self.assertEqual(self.receivers('full.yaml', labels), sorted(expected), labels)

    def test_discord_routing_is_what_it_was_without_the_ai_sre_route(self):
        for labels, _ in CASES:
            before = self.receivers('without-ai-sre.yaml', labels)
            if is_ai_sre_own(labels):  # 새로 생긴 경보 — B9b 전에는 아무 데도 가지 않았다(경로 표가 지금 수신처를 본다)
                self.assertEqual(before, ['null'], labels)
                continue
            full = [r for r in self.receivers('full.yaml', labels) if r != 'ai-sre']
            self.assertEqual(full or ['null'], before, labels)

    @unittest.skipIf(shutil.which('docker') is None, 'docker 없음')
    def test_alertmanager_starts_without_the_token_secret(self):
        # 토큰 파일이 없어도(Secret 을 아직 복사하지 않음) 설정이 읽혀 떠야 한다 — 못 뜨면 Discord 도 멈춘다. 설정이 틀리면 프로세스가 바로 끝난다
        (self.tmp / 'discord').mkdir(exist_ok=True)
        (self.tmp / 'discord/webhook-url').write_text('https://discord.invalid/api/webhooks/0/test')
        cid = run(['docker', 'run', '-d', '--rm', '-v', f'{self.tmp}/cfg:/etc/alertmanager/config:ro',
                   '-v', f'{self.tmp}/discord:/etc/alertmanager/secrets/alertmanager-discord:ro',
                   '--entrypoint', '/bin/alertmanager', AM_IMAGE,
                   '--config.file=/etc/alertmanager/config/full.yaml', '--storage.path=/tmp/am']).strip()
        try:
            status = run(['docker', 'exec', cid, 'sh', '-c',
                          'for i in $(seq 30); do wget -qO- http://localhost:9093/-/ready >/dev/null && break; sleep 1; done; '
                          'wget -qO- http://localhost:9093/api/v2/status'])
            loaded = to_json(json.loads(status)['config']['original'])[0]
            self.assertEqual([r['name'] for r in loaded['receivers']], ['null', 'discord', 'ai-sre'])
        finally:
            subprocess.run(['docker', 'rm', '-f', cid], capture_output=True)

if __name__ == '__main__':
    unittest.main()
