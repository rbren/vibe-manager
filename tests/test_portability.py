"""Portability boundaries using real files, subprocesses and source packaging."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.package_app import package


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vibe portability ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("VIBE_", "SESSION_", "OH_SESSION_", "AGENT_SERVER_", "OPENHANDS_AUTOMATION_"))}
        self.env.update(HOME=str(self.root), PYTHONPATH=str(REPO),
                        VIBE_STORE_DIR=str(self.root / "store"),
                        VIBE_DB_PATH=str(self.root / "db.sqlite"), VIBE_DATA_DIR=str(self.root / "data"))

    def run_python(self, code):
        result = subprocess.run([sys.executable, "-c", code], cwd=self.root, env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_no_guessed_services_origins_or_credentials(self):
        (self.root / ".session-key").write_text("DO_NOT_DISCOVER")
        self.run_python("""
import app
from automation import vibestore
assert not any((app.AGENT_SERVER, app.AUTOMATION_API, app.VIBE_API, app.CANVAS_BASE))
assert app.CORS_ORIGINS == [] and app.SESSION_KEY == app.AUTOMATION_KEY == ''
assert vibestore.canvas_base() == ''
for fn in (vibestore.session_key, vibestore.agent_server_url):
    try:
        fn()
    except RuntimeError as e:
        assert 'Configure' in str(e)
    else:
        raise AssertionError('must require explicit configuration')
""")

    def test_explicit_urls_and_credential_file(self):
        key = self.root / "credential"
        key.write_text("synthetic-only")
        self.env.update(VIBE_AGENT_SERVER="https://agent.example/", VIBE_CANVAS_BASE="https://canvas.example/",
                        VIBE_AUTOMATION_API="https://automation.example/api/automation/",
                        VIBE_SELF_URL="https://board.example/", VIBE_SESSION_KEY_FILE=str(key))
        self.run_python("""
import app
from automation import vibestore
assert app.AGENT_SERVER == vibestore.agent_server_url() == 'https://agent.example'
assert app.CANVAS_BASE == vibestore.canvas_base() == 'https://canvas.example'
assert app.CORS_ORIGINS == ['https://canvas.example']
assert app.AUTOMATION_API == 'https://automation.example/api/automation'
assert app.VIBE_API == 'https://board.example'
assert app.SESSION_KEY == vibestore.session_key() == 'synthetic-only'
""")

    def test_optional_local_skill_and_no_workspace_lookup(self):
        (self.root / "SKILL.md").write_text("PROJECT_POLICY_MUST_NOT_LOAD")
        self.run_python("""
from automation.vibestore import manager_skill_prompt
assert manager_skill_prompt('task', 'manager') == 'task'
""")
        skill = self.root / ".openhands/apps/kanban-manager/skills/manager/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("LOCAL_SKILL_ONLY")
        self.run_python("""
from automation.vibestore import manager_skill_prompt
for role in ('manager', 'manager_chat'):
    assert 'LOCAL_SKILL_ONLY' in manager_skill_prompt('task', role)
assert manager_skill_prompt('task', 'worker') == 'task'
""")
        self.env["VIBE_MANAGER_SKILL_FILE"] = ""
        self.run_python("""
from automation.vibestore import manager_skill_prompt
assert manager_skill_prompt('task', 'manager') == 'task'
""")
        self.env["VIBE_MANAGER_SKILL_FILE"] = str(self.root / "missing.md")
        self.run_python("""
from automation.vibestore import manager_skill_prompt
try:
    manager_skill_prompt('task', 'manager')
except FileNotFoundError:
    pass
else:
    raise AssertionError('explicit missing skill must not be silently ignored')
assert manager_skill_prompt('task', 'worker') == 'task'
""")

    def test_local_skill_is_bounded_and_not_shipped_in_automation(self):
        skill = self.root / "operator.md"
        self.env["VIBE_MANAGER_SKILL_FILE"] = str(skill)
        skill.write_text("x" * 65537)
        self.run_python("""
from automation.vibestore import manager_skill_prompt
try:
    manager_skill_prompt('task', 'manager')
except ValueError:
    pass
else:
    raise AssertionError('oversized skill accepted')
""")
        skill.write_text("PRIVATE_POLICY_SENTINEL")
        self.env.update(VIBE_AGENT_SERVER="https://agent.example", VIBE_SESSION_KEY="SYNTHETIC_SECRET",
                        VIBE_CANVAS_BASE="https://canvas.example")
        self.run_python("""
import app, io, json, tarfile
from pathlib import Path
from automation import vibestore
cli = vibestore.install_cli('workspace', '/projects/a project')
config = json.loads(Path(cli).with_name('config.json').read_text())
assert config['agent_server'] == 'https://agent.example'
assert config['canvas_base'] == 'https://canvas.example'
assert config['manager_skill_file'].endswith('operator.md')
assert 'SYNTHETIC_SECRET' not in json.dumps(config)
archive = app.build_manager_tarball({'id':'workspace', 'path':'/projects/a project', 'name':'a project'})
with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
    assert set(tar.getnames()) == {'config.json', 'main.py', 'vibectl.py', 'vibestore.py'}
    for item in tar:
        data = tar.extractfile(item).read()
        assert b'PRIVATE_POLICY_SENTINEL' not in data and b'SYNTHETIC_SECRET' not in data
""")

    def test_sidecar_skill_and_automation_configuration(self):
        root = self.root / "custom sidecar"
        skill = root / "skills/manager/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("SIDECAR_POLICY_ONLY")
        key = self.root / "automation-key"
        key.write_text("SYNTHETIC_AUTOMATION_KEY")
        (root / "integration.json").write_text(json.dumps({
            "automation_api": "https://automation.example/api/automation",
            "agent_server": "https://agent.example", "canvas_base": "https://canvas.example",
            "automation_key_file": str(key), "session_key_file": "/private/session-key",
        }))
        self.env["VIBE_SIDECAR_ROOT"] = str(root)
        self.run_python("""
import io, json, tarfile
from automation import vibestore
from scripts import push_automation
assert 'SIDECAR_POLICY_ONLY' in vibestore.manager_skill_prompt('task', 'manager')
assert push_automation.AUTOMATION_API == 'https://automation.example/api/automation'
assert push_automation.api_key() == 'SYNTHETIC_AUTOMATION_KEY'
archive = push_automation.build_tarball({'id':'workspace', 'path':'/project', 'name':'project'})
with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
    config = json.load(tar.extractfile('config.json'))
    assert config['agent_server'] == 'https://agent.example'
    assert config['canvas_base'] == 'https://canvas.example'
    assert config['session_key_file'] == '/private/session-key'
    for item in tar:
        data = tar.extractfile(item).read()
        assert b'SYNTHETIC_AUTOMATION_KEY' not in data and b'SIDECAR_POLICY_ONLY' not in data
""")

    def test_package_contains_no_local_policy_or_state(self):
        destination = self.root / "published"
        package(destination)
        for path in destination.rglob('*'):
            if not path.is_file():
                continue
            self.assertNotIn(path.name, ('SKILL.md', 'AGENTS.md', 'integration.json', 'config.json', '.session-key'))
            self.assertNotIn(path.suffix, ('.db', '.sqlite'))
            text = path.read_text()
            for local in ('canvas.rbren.io', '/root/git/', 'gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra'):
                self.assertNotIn(local, text, str(path.relative_to(destination)))


if __name__ == '__main__':
    unittest.main()
