import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent / 'secrets.sh'

# kubectl 대역: create 는 -n 과 이름, 표준입력을 한 덩어리로 내보내고, apply 는 받은 덩어리를 기록 파일에 덧붙인다. 진짜 클러스터는 건드리지 않는다.
STUB = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
log = os.environ['STUB_LOG']
if args[:2] == ['get', 'ns']:
    sys.exit(1 if args[2] in os.environ.get('STUB_MISSING_NS', '').split(',') else 0)
if 'create' in args:
    ns = args[args.index('-n') + 1]
    kind = args[args.index('create') + 1]
    name = args[args.index('create') + (3 if kind == 'secret' else 2)]
    sys.stdout.write('---\nns=%s kind=%s name=%s\n%s' % (ns, kind, name, sys.stdin.read()))
    sys.exit(0)
if args[:1] == ['apply']:
    with open(log, 'a') as f:
        f.write(sys.stdin.read())
    sys.exit(0)
sys.exit(2)
'''

CLAUDE = 'claude-value-0123456789abcdef'
GITHUB = 'github-value-0123456789abcdef'
HMAC = 'ab' * 32
VALUES = [CLAUDE, GITHUB, HMAC]


class SecretsScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / 'bin').mkdir()
        stub = self.dir / 'bin' / 'kubectl'
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.log = self.dir / 'applied'
        self.env_file = self.dir / 'env'

    def tearDown(self):
        self.tmp.cleanup()

    def write_env(self, lines=None, mode=0o600):
        lines = lines or [f'CLAUDE_CODE_OAUTH_TOKEN={CLAUDE}', f'GH_TOKEN={GITHUB}', f'RUNNER_HMAC_KEY={HMAC}', 'ISSUED=2026-11-05']
        self.env_file.write_text('\n'.join(lines) + '\n')
        self.env_file.chmod(mode)

    def run_script(self, **extra):
        env = {**os.environ, 'PATH': f'{self.dir / "bin"}:{os.environ["PATH"]}', 'ENV_FILE': str(self.env_file), 'STUB_LOG': str(self.log), **extra}
        return subprocess.run(['bash', str(SCRIPT)], env=env, text=True, capture_output=True)

    def applied(self):
        chunks = [c for c in self.log.read_text().split('---\n') if c.strip()] if self.log.exists() else []
        result = {}
        for chunk in chunks:
            header, *body = chunk.splitlines()
            fields = dict(part.split('=', 1) for part in header.split())
            result[(fields['ns'], fields['name'])] = sorted(body)
        return result

    def test_creates_four_objects_from_the_env_file_only(self):
        # given
        self.write_env()
        # when
        result = self.run_script()
        # then
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.applied(), {
            ('skills-runner', 'runner-claude'): [f'CLAUDE_CODE_OAUTH_TOKEN={CLAUDE}'],
            ('skills-runner', 'runner-github'): [f'GH_TOKEN={GITHUB}'],
            ('skills-runner', 'runner-hmac'): [f'hmac-key={HMAC}'],
            ('skills', 'runner-hmac'): [f'hmac-key={HMAC}'],
            ('skills-runner', 'runner-token-issued'): ['ISSUED=2026-11-05'],
        })

    def test_never_prints_a_secret_value(self):
        # given
        self.write_env()
        # when
        result = self.run_script()
        # then
        for value in VALUES:
            self.assertNotIn(value, result.stdout)
            self.assertNotIn(value, result.stderr)

    def test_failures_do_not_print_values_either(self):
        # given
        cases = {
            'mode': dict(mode=0o644),
            'extra line': dict(lines=[f'CLAUDE_CODE_OAUTH_TOKEN={CLAUDE}', f'GH_TOKEN={GITHUB}', f'RUNNER_HMAC_KEY={HMAC}', 'ISSUED=2026-11-05', f'ANTHROPIC_API_KEY={CLAUDE}']),
            'short hmac': dict(lines=[f'CLAUDE_CODE_OAUTH_TOKEN={CLAUDE}', f'GH_TOKEN={GITHUB}', 'RUNNER_HMAC_KEY=abcd', 'ISSUED=2026-11-05']),
            'bad date': dict(lines=[f'CLAUDE_CODE_OAUTH_TOKEN={CLAUDE}', f'GH_TOKEN={GITHUB}', f'RUNNER_HMAC_KEY={HMAC}', 'ISSUED=yesterday']),
            'api key instead of github': dict(lines=[f'CLAUDE_CODE_OAUTH_TOKEN={CLAUDE}', f'ANTHROPIC_API_KEY={GITHUB}', f'RUNNER_HMAC_KEY={HMAC}', 'ISSUED=2026-11-05']),
            'duplicate key': dict(lines=[f'CLAUDE_CODE_OAUTH_TOKEN={CLAUDE}', f'CLAUDE_CODE_OAUTH_TOKEN={CLAUDE}', f'RUNNER_HMAC_KEY={HMAC}', 'ISSUED=2026-11-05']),
            'space in value': dict(lines=[f'CLAUDE_CODE_OAUTH_TOKEN={CLAUDE} x', f'GH_TOKEN={GITHUB}', f'RUNNER_HMAC_KEY={HMAC}', 'ISSUED=2026-11-05']),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                self.write_env(**kwargs)
                # when
                result = self.run_script()
                # then
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log.exists())
                for value in VALUES:
                    self.assertNotIn(value, result.stdout + result.stderr)

    def test_missing_file_or_namespace_stops_before_any_write(self):
        # given
        missing_file = self.run_script()
        self.write_env()
        # when
        missing_ns = self.run_script(STUB_MISSING_NS='skills')
        # then
        self.assertNotEqual(missing_file.returncode, 0)
        self.assertNotEqual(missing_ns.returncode, 0)
        self.assertIn('namespace skills', missing_ns.stderr)
        self.assertFalse(self.log.exists())

    def test_can_run_twice(self):
        # given
        self.write_env()
        # when
        first = self.run_script()
        second = self.run_script()
        # then
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(len(self.log.read_text().split('---\n')) - 1, 10)
