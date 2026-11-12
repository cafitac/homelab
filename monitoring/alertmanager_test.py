# Alertmanager 설정을 차트로 렌더해 검사한다(클러스터에 닿지 않는다). 실행: python3 -m unittest monitoring/alertmanager_test.py
# 필요: helm · yq · docker(amtool 이 PATH 에 있으면 그것을 쓰고, 없으면 아래 고정 이미지). CI: .github/workflows/amtool.yml
# 핵심 약속: ai-sre 경로는 맨 앞 + continue 라 Discord 로 가는 경보는 ai-sre 경로가 없을 때와 똑같다.
import base64
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
TOKEN_DIR = '/etc/alertmanager/ai-sre'

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
    ({'alertname': 'AiSreWebhookErrors', 'severity': 'critical', 'namespace': 'ai-sre'}, ['null']),
    ({'alertname': 'KubePodCrashLooping', 'severity': 'warning', 'namespace': 'ai-sre'}, ['null']),
]


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
        cls.spec = next(d for d in docs if d['kind'] == 'Alertmanager')['spec']
        (cls.tmp / 'cfg').mkdir()
        (cls.tmp / 'cfg/full.yaml').write_text(cls.config_text)
        stripped = dict(cls.config, route=dict(cls.config['route'], routes=cls.config['route']['routes'][1:]),
                        receivers=[r for r in cls.config['receivers'] if r['name'] != 'ai-sre'])
        (cls.tmp / 'cfg/without-ai-sre.yaml').write_text(json.dumps(stripped, ensure_ascii=False))

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
        self.assertEqual(hook['http_config'], {'authorization': {'type': 'Bearer', 'credentials_file': f'{TOKEN_DIR}/token'}})

    def test_token_volume_is_optional_so_a_missing_secret_cannot_stop_alertmanager(self):
        self.assertEqual(self.spec['secrets'], ['alertmanager-discord'])
        (vol,) = self.spec['volumes']
        self.assertEqual(vol['secret'], {'secretName': 'ai-sre-alertmanager-token', 'optional': True})
        self.assertEqual(self.spec['volumeMounts'], [{'name': vol['name'], 'mountPath': TOKEN_DIR, 'readOnly': True}])

    def test_routing_table(self):
        for labels, expected in CASES:
            self.assertEqual(self.receivers('full.yaml', labels), sorted(expected), labels)

    def test_discord_routing_is_what_it_was_without_the_ai_sre_route(self):
        for labels, _ in CASES:
            full = [r for r in self.receivers('full.yaml', labels) if r != 'ai-sre']
            self.assertEqual(full or ['null'], self.receivers('without-ai-sre.yaml', labels), labels)

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
