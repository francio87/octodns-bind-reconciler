import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from octodns_reconciler import (
    AppliedState,
    CommandRunner,
    Config,
    ConfigurationError,
    GitRepository,
    Reconciler,
    ReconciliationError,
    run_service,
)


class ConfigTests(unittest.TestCase):
    def test_requires_repository_url(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "REPOSITORY_URL is required"):
                Config.from_env()

    def test_rejects_credentials_embedded_in_repository_url(self):
        environment = {
            "REPOSITORY_URL": "https://user:secret@github.com/example/config.git",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "must not contain credentials"):
                Config.from_env()

    def test_rejects_unsupported_repository_transport(self):
        environment = {
            "REPOSITORY_URL": "ssh://git@github.com/example/config.git",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "must use HTTPS"):
                Config.from_env()

    def test_rejects_repository_url_query_or_fragment(self):
        for url in (
            "https://github.com/example/config.git?token=secret",
            "https://github.com/example/config.git#secret",
        ):
            with self.subTest(url=url), patch.dict(
                os.environ, {"REPOSITORY_URL": url}, clear=True
            ):
                with self.assertRaisesRegex(ConfigurationError, "query or fragment"):
                    Config.from_env()

    def test_rejects_nonpositive_interval(self):
        environment = {
            "REPOSITORY_URL": "https://github.com/example/config.git",
            "POLL_INTERVAL_SECONDS": "0",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "POLL_INTERVAL_SECONDS must be positive"):
                Config.from_env()

    def test_loads_runtime_settings_with_safe_defaults(self):
        environment = {
            "REPOSITORY_URL": "https://github.com/example/dns-config.git",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_env()

        self.assertEqual("main", config.repository_branch)
        self.assertEqual("config-internal.yaml", config.octodns_config_file)
        self.assertEqual(300, config.poll_interval_seconds)
        self.assertEqual(60, config.retry_interval_seconds)
        self.assertEqual(120, config.command_timeout_seconds)
        self.assertEqual("/data/repository", str(config.worktree))
        self.assertEqual("/data/last-applied-commit", str(config.state_file))
        self.assertFalse(config.run_once)


class CommandRunnerTests(unittest.TestCase):
    def test_failed_command_logs_captured_output(self):
        runner = CommandRunner(timeout_seconds=10)

        with self.assertLogs("octodns-reconciler", level="ERROR") as logs:
            with self.assertRaises(subprocess.CalledProcessError):
                runner.run(["sh", "-c", "printf 'validation failed\\n'; exit 7"])

        self.assertIn("validation failed", "\n".join(logs.output))

    def test_scopes_git_and_octodns_credentials(self):
        runner = CommandRunner(timeout_seconds=10)
        environment = {
            "PATH": os.environ["PATH"],
            "GIT_TOKEN": "git-secret",
            "GIT_USERNAME": "git-user",
            "OCTODNS_TSIG_SECRET": "dns-secret",
            "OCTODNS_TSIG_NAME": "dns-key",
            "GIT_EXEC_PATH": "/untrusted/git-exec",
            "GIT_COMMON_DIR": "/untrusted/git-common",
            "GIT_TEMPLATE_DIR": "/untrusted/git-template",
        }
        command = [
            "python3",
            "-c",
            "import json,os; print(json.dumps(dict(os.environ)))",
        ]

        with patch.dict(os.environ, environment, clear=True):
            git_environment = __import__("json").loads(
                runner.run(command, scope="git")
            )
            octodns_environment = __import__("json").loads(
                runner.run(command, scope="octodns")
            )

        self.assertIn("GIT_TOKEN", git_environment)
        self.assertNotIn("OCTODNS_TSIG_SECRET", git_environment)
        self.assertEqual(os.devnull, git_environment["GIT_CONFIG_GLOBAL"])
        self.assertEqual("1", git_environment["GIT_CONFIG_NOSYSTEM"])
        self.assertEqual("https", git_environment["GIT_ALLOW_PROTOCOL"])
        self.assertEqual("/dev/null", git_environment["GIT_CONFIG_VALUE_0"])
        for name in ("GIT_EXEC_PATH", "GIT_COMMON_DIR", "GIT_TEMPLATE_DIR"):
            self.assertNotIn(name, git_environment)
        self.assertIn("OCTODNS_TSIG_SECRET", octodns_environment)
        self.assertNotIn("GIT_TOKEN", octodns_environment)


class AppliedStateTests(unittest.TestCase):
    def test_missing_state_is_empty_and_saved_commit_is_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            state = AppliedState(Path(directory) / "last-applied-commit")

            self.assertIsNone(state.load())
            state.save("abc123")
            self.assertEqual("abc123", state.load())

    def test_rejects_symlinked_state_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("abc123\n", encoding="utf-8")
            state_path = root / "last-applied-commit"
            state_path.symlink_to(target)

            with self.assertRaisesRegex(ConfigurationError, "state file must not be a symlink"):
                AppliedState(state_path)


class GitRepositoryTests(unittest.TestCase):
    def test_rejects_config_path_outside_checkout(self):
        with self.assertRaisesRegex(ConfigurationError, "must stay inside"):
            GitRepository(
                repository_url="https://github.com/example/config.git",
                branch="main",
                worktree=Path("/data/repository"),
                config_file="../outside.yaml",
                runner=None,
            )

    def test_rejects_symlinked_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual"
            (actual / ".git").mkdir(parents=True)
            (actual / "config-internal.yaml").write_text("zones: {}\n", encoding="utf-8")
            worktree = root / "repository"
            worktree.symlink_to(actual, target_is_directory=True)

            class Runner:
                def run(self, command, cwd=None, scope=None):
                    if command[:3] == ["git", "remote", "get-url"]:
                        return "https://github.com/example/config.git\n"
                    if command[:3] == ["git", "rev-parse", "HEAD"]:
                        return "abc123\n"
                    return ""

            repository = GitRepository(
                repository_url="https://github.com/example/config.git",
                branch="main",
                worktree=worktree,
                config_file="config-internal.yaml",
                runner=Runner(),
            )

            with self.assertRaisesRegex(ConfigurationError, "worktree must not be a symlink"):
                repository.update()

    def test_rejects_config_symlink_escaping_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "repository"
            (worktree / ".git").mkdir(parents=True)
            outside = root / "outside.yaml"
            outside.write_text("zones: {}\n", encoding="utf-8")
            (worktree / "config-internal.yaml").symlink_to(outside)

            class Runner:
                def run(self, command, cwd=None, scope=None):
                    if command[:3] == ["git", "remote", "get-url"]:
                        return "https://github.com/example/config.git\n"
                    if command[:3] == ["git", "rev-parse", "HEAD"]:
                        return "abc123\n"
                    return ""

            repository = GitRepository(
                repository_url="https://github.com/example/config.git",
                branch="main",
                worktree=worktree,
                config_file="config-internal.yaml",
                runner=Runner(),
            )

            with self.assertRaisesRegex(ConfigurationError, "must stay inside"):
                repository.update()

    def test_rejects_symlinks_anywhere_in_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "repository"
            (worktree / ".git").mkdir(parents=True)
            (worktree / "config-internal.yaml").write_text("zones: {}\n", encoding="utf-8")
            (worktree / "zones").mkdir()
            outside = root / "outside.yaml"
            outside.write_text("secret", encoding="utf-8")
            (worktree / "zones" / "linked.yaml").symlink_to(outside)

            class Runner:
                def run(self, command, cwd=None, scope=None):
                    if command[:3] == ["git", "remote", "get-url"]:
                        return "https://github.com/example/config.git\n"
                    if command[:3] == ["git", "rev-parse", "HEAD"]:
                        return "abc123\n"
                    return ""

            repository = GitRepository(
                repository_url="https://github.com/example/config.git",
                branch="main",
                worktree=worktree,
                config_file="config-internal.yaml",
                runner=Runner(),
            )

            with self.assertRaisesRegex(ConfigurationError, "symlinks are not allowed"):
                repository.update()

    def test_failed_clone_does_not_leave_partial_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "repository"

            class Runner:
                def run(self, command, cwd=None, scope=None):
                    destination = Path(command[-1])
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / "partial").write_text("incomplete", encoding="utf-8")
                    raise subprocess.CalledProcessError(128, command)

            repository = GitRepository(
                repository_url="https://github.com/example/config.git",
                branch="main",
                worktree=worktree,
                config_file="config-internal.yaml",
                runner=Runner(),
            )

            with self.assertRaises(subprocess.CalledProcessError):
                repository.update()

            self.assertFalse(worktree.exists())
            self.assertEqual([], list(root.iterdir()))

    def test_clones_requested_branch_and_returns_config_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin.git"
            seed = root / "seed"
            subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
            subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "commit.gpgsign", "false"], check=True)
            (seed / "config-internal.yaml").write_text("zones: {}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "config"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(origin)], check=True)
            subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)

            worktree = root / "data" / "repository"
            repository = GitRepository(
                repository_url=str(origin),
                branch="main",
                worktree=worktree,
                config_file="config-internal.yaml",
                runner=CommandRunner(timeout_seconds=10, git_allow_protocol="file"),
            )

            commit, config_file = repository.update()

            expected_commit = subprocess.run(
                ["git", "-C", str(seed), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(expected_commit, commit)
            self.assertEqual(worktree / "config-internal.yaml", config_file)

            subprocess.run(["git", "-C", str(seed), "checkout", "-b", "alternate"], check=True, capture_output=True)
            (seed / "config-internal.yaml").write_text("zones:\n  alternate: {}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-m", "alternate config"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(seed), "push", "origin", "alternate"], check=True, capture_output=True)

            alternate_repository = GitRepository(
                repository_url=str(origin),
                branch="alternate",
                worktree=worktree,
                config_file="config-internal.yaml",
                runner=CommandRunner(timeout_seconds=10, git_allow_protocol="file"),
            )
            alternate_commit, _ = alternate_repository.update()
            expected_alternate = subprocess.run(
                ["git", "-C", str(seed), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(expected_alternate, alternate_commit)

    def test_existing_checkout_is_verified_and_fast_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "repository"
            (worktree / ".git").mkdir(parents=True)
            (worktree / "config-internal.yaml").write_text("zones: {}\n", encoding="utf-8")

            class Runner:
                def __init__(self):
                    self.commands = []

                def run(self, command, cwd=None, scope=None):
                    self.commands.append((command, cwd))
                    if command[:3] == ["git", "remote", "get-url"]:
                        return "https://github.com/example/config.git\n"
                    if command[:3] == ["git", "rev-parse", "HEAD"]:
                        return "def456\n"
                    return ""

            runner = Runner()
            repository = GitRepository(
                repository_url="https://github.com/example/config.git",
                branch="main",
                worktree=worktree,
                config_file="config-internal.yaml",
                runner=runner,
            )

            commit, _ = repository.update()

            self.assertEqual("def456", commit)
            self.assertEqual(
                [
                    (["git", "remote", "get-url", "origin"], worktree),
                    (["git", "status", "--porcelain"], worktree),
                    (["git", "fetch", "--prune", "origin", "main"], worktree),
                    (["git", "checkout", "main"], worktree),
                    (["git", "merge", "--ff-only", "origin/main"], worktree),
                    (["git", "rev-parse", "HEAD"], worktree),
                ],
                runner.commands,
            )


class ReconcilerTests(unittest.TestCase):
    def test_changed_commit_is_validated_planned_applied_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config-internal.yaml"
            config_file.write_text("zones: {}\n", encoding="utf-8")

            class Repository:
                def update(self):
                    return "abc123", config_file

            class State:
                def __init__(self):
                    self.saved = []

                def load(self):
                    return None

                def save(self, commit):
                    self.saved.append(commit)

            class Runner:
                def __init__(self):
                    self.commands = []

                def run(self, command, cwd=None, scope=None):
                    self.commands.append((command, cwd))
                    if len(self.commands) == 4:
                        return "No changes were planned"
                    return ""

            state = State()
            runner = Runner()
            reconciler = Reconciler(Repository(), state, runner)
            changed = reconciler.reconcile_once()

            option = f"--config-file={config_file}"
            self.assertTrue(changed)
            self.assertEqual(
                [
                    (["octodns-validate", option, "--all"], config_file.parent),
                    (["octodns-sync", option], config_file.parent),
                    (["octodns-sync", option, "--doit"], config_file.parent),
                    (["octodns-sync", option], config_file.parent),
                ],
                runner.commands,
            )
            self.assertEqual(["abc123"], state.saved)

    def test_verification_with_remaining_changes_does_not_mark_commit_applied(self):
        class Repository:
            def update(self):
                return "abc123", Path("/tmp/config-internal.yaml")

        class State:
            def __init__(self):
                self.saved = []

            def load(self):
                return None

            def save(self, commit):
                self.saved.append(commit)

        class Runner:
            def __init__(self):
                self.calls = 0

            def run(self, command, cwd=None, scope=None):
                self.calls += 1
                if self.calls == 4:
                    return "Create <ARecord ...>"
                return ""

        state = State()
        with self.assertRaisesRegex(ReconciliationError, "still plans changes"):
            Reconciler(Repository(), state, Runner()).reconcile_once()

        self.assertEqual([], state.saved)

    def test_already_applied_commit_is_reconciled_for_drift(self):
        class Repository:
            def update(self):
                return "abc123", Path("/tmp/config-internal.yaml")

        class State:
            def __init__(self):
                self.saved = []

            def load(self):
                return "abc123"

            def save(self, commit):
                self.saved.append(commit)

        class Runner:
            def __init__(self):
                self.calls = 0

            def run(self, command, cwd=None, scope=None):
                self.calls += 1
                if self.calls == 4:
                    return "No changes were planned"
                return ""

        state = State()
        runner = Runner()
        changed = Reconciler(Repository(), state, runner).reconcile_once()

        self.assertTrue(changed)
        self.assertEqual(4, runner.calls)
        self.assertEqual(["abc123"], state.saved)


class ServiceTests(unittest.TestCase):
    def test_run_once_reconciles_exactly_once(self):
        class ReconcilerStub:
            def __init__(self):
                self.calls = 0

            def reconcile_once(self):
                self.calls += 1

        reconciler = ReconcilerStub()
        run_service(
            reconciler,
            run_once=True,
            poll_interval_seconds=300,
            retry_interval_seconds=60,
            sleep=lambda seconds: self.fail(f"unexpected sleep: {seconds}"),
        )

        self.assertEqual(1, reconciler.calls)


if __name__ == "__main__":
    unittest.main()
