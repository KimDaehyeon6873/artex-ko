#!/usr/bin/env python3
"""VM 없이 설치 스크립트의 자격 증명 보존과 실패/재기동 흐름을 검증한다.

실행: python3 -I scripts/test-local-vm.py
실제 Docker 호출 대신 격리된 임시 디렉터리의 테스트 더블을 사용한다.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MOCK_DOCKER = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with open(os.environ["MOCK_DOCKER_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "shadow": any(
        key in os.environ for key in (
            "ARTEX_VM_DB_PASSWORD", "ARTEX_VM_HTTP_PORT", "ARTEX_VM_PROXY_PORT"
        ))}) + "\n")
if args == ["compose", "version"]:
    print("Docker Compose version v2.20.0")
elif args == ["compose", "up", "--help"]:
    print("--wait-timeout" if not os.environ.get("MOCK_OLD_COMPOSE") else "--detach")
elif args == ["info"]:
    sys.exit(1 if os.environ.get("MOCK_DAEMON_FAILURE") else 0)
elif args[:2] == ["volume", "ls"]:
    print(os.environ.get("MOCK_EXISTING_VOLUMES", ""))
elif args and args[0] == "compose":
    assert args[1:3] == ["--project-name", "artex-local-vm"], args
    env_file = Path(args[args.index("--env-file") + 1])
    command = args[args.index("-f") + 2:]
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                  if "=" in line and not line.startswith("#"))
    if command == ["config", "--quiet"]:
        sys.exit(1 if os.environ.get("MOCK_CONFIG_FAILURE") else 0)
    if command[0] == "build":
        sys.exit(1 if os.environ.get("MOCK_BUILD_FAILURE") else 0)
    if command[0] == "pull":
        sys.exit(1 if os.environ.get("MOCK_PULL_FAILURE") else 0)
    if command[0] == "up":
        sys.exit(1 if os.environ.get("MOCK_HEALTH_FAILURE") else 0)
    if command == ["port", "artex", "8787"]:
        print("127.0.0.1:" + values.get("ARTEX_VM_HTTP_PORT", "8787"))
    elif command[0] not in ("ps", "stop", "down", "logs"):
        raise AssertionError(command)
else:
    raise AssertionError(args)
'''


class LocalVMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="artex vm test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copy2(ROOT / "local-vm.sh", self.root / "local-vm.sh")
        shutil.copy2(ROOT / "compose.local-vm.yaml", self.root / "compose.local-vm.yaml")
        mock_bin = self.root / "bin"
        mock_bin.mkdir()
        docker = mock_bin / "docker"
        docker.write_text(MOCK_DOCKER)
        docker.chmod(0o755)
        self.log = self.root / "docker.jsonl"
        self.env_file = self.root / ".local-vm" / "compose.env"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("MOCK_", "ARTEX_VM_"))}
        self.env.update(PATH=str(mock_bin) + os.pathsep + os.environ["PATH"],
                        MOCK_DOCKER_LOG=str(self.log))

    def run_script(self, *args, **env):
        return subprocess.run(
            ["bash", str(self.root / "local-vm.sh"), *args], cwd="/tmp",
            env={**self.env, **env}, text=True, capture_output=True, timeout=20,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def actions(self):
        return [call["args"][call["args"].index("-f") + 2:]
                for call in self.calls() if "-f" in call["args"]]

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_up_builds_before_launch_and_never_prints_credentials(self):
        result = self.run_script()
        self.assert_success(result)
        content = self.env_file.read_text()
        password = re.search(r"ARTEX_VM_DB_PASSWORD=([0-9a-f]{64})\n", content)[1]
        self.assertNotIn(password, result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE(self.env_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.env_file.parent.stat().st_mode), 0o700)
        self.assertEqual([action[0] for action in self.actions()],
                         ["config", "build", "pull", "up", "port"])
        launch = next(action for action in self.actions() if action[0] == "up")
        self.assertIn("--wait", launch)
        self.assertIn("--no-build", launch)
        self.assertIn("http://127.0.0.1:8787", result.stdout)
        self.assertFalse((self.env_file.parent / "lock").exists())

    def test_rerun_preserves_credentials_and_existing_native_configuration(self):
        for name in ("config.json", ".env", "jwt.key"):
            (self.root / name).write_text("existing setting: " + name)
        self.assert_success(self.run_script())
        original = self.env_file.read_bytes()
        self.assert_success(self.run_script())
        self.assertEqual(self.env_file.read_bytes(), original)
        for name in ("config.json", ".env", "jwt.key"):
            self.assertEqual((self.root / name).read_text(), "existing setting: " + name)

    def test_build_failure_does_not_launch_or_erase_state(self):
        result = self.run_script(MOCK_BUILD_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([action[0] for action in self.actions()], ["config", "build"])
        original = self.env_file.read_bytes()
        self.assert_success(self.run_script())
        self.assertEqual(self.env_file.read_bytes(), original)

    def test_missing_credentials_for_existing_volumes_fails_without_regeneration(self):
        result = self.run_script(MOCK_EXISTING_VOLUMES="artex-local-vm_pgdata")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("복원", result.stderr)
        self.assertFalse(self.env_file.exists())
        self.assertEqual(self.actions(), [])

    def test_failed_health_check_is_not_reported_as_success(self):
        result = self.run_script(MOCK_HEALTH_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("준비 완료", result.stdout)
        self.assertIn("logs", result.stderr)
        self.assertEqual(self.actions()[-1], ["ps"])
        self.assertTrue(self.env_file.exists())

    def test_start_stop_down_preserve_credentials_without_build_or_volume_removal(self):
        self.assert_success(self.run_script("build"))
        original = self.env_file.read_bytes()
        self.log.write_text("")
        for command in ("start", "stop", "down"):
            self.assert_success(self.run_script(command))
        actions = self.actions()
        self.assertNotIn("build", [action[0] for action in actions])
        self.assertNotIn("pull", [action[0] for action in actions])
        self.assertIn(["down"], actions)
        self.assertFalse(any("--volumes" in action or "-v" in action for action in actions))
        self.assertEqual(self.env_file.read_bytes(), original)

    def test_host_environment_cannot_override_saved_database_password(self):
        result = self.run_script(ARTEX_VM_DB_PASSWORD="incorrect-host-password",
                                 ARTEX_VM_HTTP_PORT="9999", ARTEX_VM_PROXY_PORT="9998")
        self.assert_success(result)
        self.assertTrue(all(not call["shadow"] for call in self.calls() if "-f" in call["args"]))
        self.assertIn("127.0.0.1:8787", result.stdout)

    def test_environment_file_is_not_executed_as_shell(self):
        self.assert_success(self.run_script("build"))
        marker = self.root / "executed"
        with self.env_file.open("a") as env:
            env.write(f'UNUSED=$(touch "{marker}")\n')
        self.assert_success(self.run_script("start"))
        self.assertFalse(marker.exists())

    def test_status_and_logs_do_not_hold_or_remove_another_process_lock(self):
        self.assert_success(self.run_script("build"))
        lock = self.env_file.parent / "lock"
        lock.mkdir()
        for command in ("status", "logs"):
            self.assert_success(self.run_script(command))
            self.assertTrue(lock.is_dir())
        self.assertNotEqual(self.run_script("stop").returncode, 0)
        self.assertTrue(lock.is_dir())

    def test_read_commands_do_not_create_credentials(self):
        for command in ("start", "stop", "down", "status", "logs"):
            self.assertNotEqual(self.run_script(command).returncode, 0)
            self.assertFalse(self.env_file.exists())

    def test_preflight_failure_does_not_create_state(self):
        for failure in ("MOCK_DAEMON_FAILURE", "MOCK_OLD_COMPOSE"):
            self.assertNotEqual(self.run_script(**{failure: "1"}).returncode, 0)
            self.assertFalse(self.env_file.parent.exists())

    def test_help_and_invalid_commands_do_not_call_docker(self):
        self.assert_success(self.run_script("--help"))
        self.assertEqual(self.run_script("purge").returncode, 2)
        self.assertEqual(self.run_script("up", "--volumes").returncode, 2)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
