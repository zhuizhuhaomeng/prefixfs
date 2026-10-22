"""Unit and opt-in host checks for the generic prefixfs command runner."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "run-prefixfs.py"
SPEC = importlib.util.spec_from_file_location("prefixfs_runner", RUNNER)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class PrefixfsChecks(unittest.TestCase):
    def setUp(self):
        tools = patch.object(runner.shutil, "which", side_effect=lambda name: "/usr/bin/" + name)
        tools.start()
        self.addCleanup(tools.stop)

    def test_prefixes_are_explicit_and_independent_of_nginx(self):
        for value, expected in (("cache_,build_", ["cache_", "build_"]),
                                ("cache_, cache_,temp", ["cache_", "temp"])):
            with self.subTest(value=value), patch.dict(os.environ, {"PREFIXFS_PREFIXES": value}, clear=True):
                self.assertEqual(runner.prefixes_from_env(), expected)
        for env in ({}, {"PREFIX_TMPFS_PREFIXES": "servroot"}, {"SERVROOT_FS_PREFIXES": "servroot"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "--prefix or PREFIXFS_PREFIXES"):
                    runner.prefixes_from_env()
        self.assertEqual(runner.validate_prefixes(["cache_", "build_"]), ["cache_", "build_"])
        for prefixes in ([], [""], ["."], [".."], ["a/b"], ["a,b"], ["x" * 256],
                         [str(index) for index in range(65)]):
            with self.subTest(prefixes=prefixes):
                with self.assertRaisesRegex(RuntimeError, "directory-name prefixes"):
                    runner.validate_prefixes(prefixes)

    def test_help_describes_a_general_purpose_command(self):
        result = subprocess.run([sys.executable, str(RUNNER), "--help"],
                                capture_output=True, text=True, check=True)
        self.assertIn("current working directory", result.stdout)
        self.assertIn("--prefix", result.stdout)
        self.assertIn("--cleanup", result.stdout)
        self.assertNotIn("nginx", result.stdout.lower())

    def test_missing_module_fails_before_privileged_commands(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"PREFIXFS_KO": str(Path(directory) / "missing.ko")}), patch.object(
                runner, "module_loaded", return_value=False), patch.object(
                runner.subprocess, "run") as execute:
            with self.assertRaisesRegex(RuntimeError, runner.PROJECT_URL):
                runner.prepare_module()
            execute.assert_not_called()

    def test_wrong_kernel_does_not_load_module(self):
        with tempfile.NamedTemporaryFile(suffix=".ko") as module, patch.dict(
                os.environ, {"PREFIXFS_KO": module.name}), patch.object(
                runner, "module_loaded", return_value=False), patch.object(
                runner.subprocess, "check_output", return_value="different-kernel SMP\n"), patch.object(
                runner.subprocess, "run") as execute:
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                runner.prepare_module()
            execute.assert_not_called()

    def test_loaded_module_does_not_require_a_local_binary(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
                runner, "module_loaded", return_value=True), patch.object(
                runner.subprocess, "run") as execute:
            runner.prepare_module()
            execute.assert_not_called()

    def test_explicit_missing_module_is_checked_when_driver_is_loaded(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"PREFIXFS_KO": str(Path(directory) / "missing.ko")}), patch.object(
                runner, "module_loaded", return_value=True), patch.object(
                runner.subprocess, "run") as execute:
            with self.assertRaisesRegex(RuntimeError, "module not found"):
                runner.prepare_module()
            execute.assert_not_called()

    def test_missing_namespace_dependency_is_reported(self):
        with patch.object(runner.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "requires unshare"):
                runner.prepare_module()

    def test_disk_backing_is_rejected_before_loading_module(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            with patch.object(runner.subprocess, "check_output", return_value="ext4\n"), patch.object(
                    runner, "prepare_module") as prepare:
                with self.assertRaisesRegex(RuntimeError, "must be on tmpfs"):
                    runner.run(source, Path(directory), ["true"], prefixes=["cache_"])
                prepare.assert_not_called()


@unittest.skipUnless(os.environ.get("PREFIXFS_HOST_TESTS") == "1",
                     "set PREFIXFS_HOST_TESTS=1 to load/mount prefixfs on the host")
class HostRunner(unittest.TestCase):
    def setUp(self):
        fixtures = ROOT / "build/test-runner"
        fixtures.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(prefix="project with spaces-", dir=fixtures)
        self.addCleanup(fixture.cleanup)
        self.base = Path(fixture.name)
        self.root = self.base / "project"
        (self.root / "source").mkdir(parents=True)
        memory = tempfile.TemporaryDirectory(prefix="prefixfs-runner-test-", dir="/tmp")
        self.addCleanup(memory.cleanup)
        self.memory = Path(memory.name)

    def environment(self):
        return {key: value for key, value in os.environ.items()
                if not key.startswith("PREFIXFS_") and key != "LD_PRELOAD"}

    def retained_path(self, output):
        prefix = "prefixfs: retained diagnostics at "
        line = next(line for line in output.splitlines() if line.startswith(prefix))
        retained = Path(line[len(prefix):])
        self.assertEqual(retained.parent, self.memory)
        return retained

    def test_default_source_multiple_prefixes_and_explicit_cleanup(self):
        code = '''import pathlib, subprocess, sys
for name in ('cache_work', 'build_extra'):
    directory = pathlib.Path(name)
    directory.mkdir()
    (directory / 'value').write_text('retained data')
    kind = subprocess.check_output(['stat', '-f', '-c', '%T', name], text=True).strip()
    if kind != 'tmpfs':
        raise SystemExit(2)
pathlib.Path('ordinary-file').write_text('on original filesystem')
raise SystemExit(int(sys.argv[1]))
'''
        for cleanup, status in ((False, 0), (True, 0), (False, 7), (True, 7)):
            with self.subTest(cleanup=cleanup, status=status):
                env = self.environment()
                # These project-specific variables must not affect the runner.
                env.update(TEST_NGINX_RANDOMIZE='1', PREFIXFS_PREFIXES='ignored_')
                args = [sys.executable, str(RUNNER), '--tmpdir', str(self.memory),
                        '--prefix', 'cache_', '--prefix', 'build_']
                if cleanup:
                    args.append('--cleanup')
                args.extend(['--', sys.executable, '-c', code, str(status)])
                result = subprocess.run(args, cwd=self.root, env=env,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertFalse((self.root / 'cache_work').exists())
                self.assertFalse((self.root / 'build_extra').exists())
                self.assertEqual((self.root / 'ordinary-file').read_text(), 'on original filesystem')
                if cleanup and status == 0:
                    self.assertNotIn('retained diagnostics at', result.stderr)
                else:
                    retained = self.retained_path(result.stderr)
                    self.assertEqual((retained / 'memory/cache_work/value').read_text(), 'retained data')
                    self.assertEqual((retained / 'memory/build_extra/value').read_text(), 'retained data')

    def test_mount_only_affects_selected_directory_in_test_namespace(self):
        other = self.base / "other-project/source"
        other.mkdir(parents=True)
        ready = self.base / "scope.json"
        release = self.base / "release-scope-probe"
        code = '''import json, os, pathlib, subprocess, sys, time
source, other, ready, release = map(pathlib.Path, sys.argv[1:])
for directory in (source / 'cache_scope', other / 'cache_scope'):
    directory.mkdir()
    (directory / 'value').write_text('scope probe')
filesystems = [subprocess.check_output(['stat', '-f', '-c', '%T', str(directory)], text=True).strip()
               for directory in (source / 'cache_scope', other / 'cache_scope')]
ready.write_text(json.dumps(filesystems))
print('READY', flush=True)
while not release.exists():
    time.sleep(0.01)
'''
        process = subprocess.Popen(
            [sys.executable, str(RUNNER), "--source", str(self.root / "source"),
             "--tmpdir", str(self.memory), "--prefix", "cache_", "--cleanup", "--", sys.executable, "-c", code,
             str(self.root / "source"), str(other), str(ready), str(release)],
            cwd=self.root, env=self.environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(process.stdout.readline().startswith("prefixfs:"))
            self.assertEqual(process.stdout.readline().strip(), "READY")
            # Check while the mount is active, from the original namespace.
            self.assertFalse((self.root / "source/cache_scope").exists())
            self.assertEqual((other / "cache_scope/value").read_text(), "scope probe")
            original_kind = subprocess.check_output(
                ["stat", "-f", "-c", "%T", str(other)], text=True).strip()
            self.assertNotEqual(original_kind, "tmpfs", "scope fixture must be disk-backed")
            self.assertEqual(json.loads(ready.read_text()), ["tmpfs", original_kind])
            release.touch()
            _, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(list(self.memory.iterdir()), [])
        finally:
            release.touch()
            runner.stop(process)
            process.stdout.close()
            process.stderr.close()

    def test_interruption_stops_namespace_and_preserves_data(self):
        env = self.environment()
        process = subprocess.Popen(
            [sys.executable, str(RUNNER), "--source", str(self.root / "source"),
             "--tmpdir", str(self.memory), "--prefix", "cache_", "--cleanup", "--", "bash", "-c",
             "mkdir -p source/cache_work/logs; echo diagnostics > source/cache_work/logs/error.log; echo READY; sleep 60"],
            cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(process.stdout.readline().startswith("prefixfs:"))
            self.assertEqual(process.stdout.readline().strip(), "READY")
            process.terminate()
            _, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 143, error)
            retained = self.retained_path(error)
            self.assertEqual((retained / "memory/cache_work/logs/error.log").read_text(), "diagnostics\n")
            self.assertFalse((self.root / "source/cache_work").exists())
        finally:
            runner.stop(process)
            process.stdout.close()
            process.stderr.close()


if __name__ == '__main__':
    unittest.main()
