import fcntl
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


LOG = logging.getLogger("octodns-reconciler")


class ConfigurationError(ValueError):
    pass


class ReconciliationError(RuntimeError):
    pass


def _reject_symlink_components(path: Path, label: str) -> None:
    absolute_path = path.absolute()
    for component in (absolute_path, *absolute_path.parents):
        if component.is_symlink():
            raise ConfigurationError(f"{label} must not be a symlink")


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Config:
    repository_url: str
    repository_branch: str
    octodns_config_file: str
    poll_interval_seconds: int
    retry_interval_seconds: int
    command_timeout_seconds: int
    worktree: Path
    state_file: Path
    run_once: bool

    @classmethod
    def from_env(cls) -> "Config":
        repository_url = os.environ.get("REPOSITORY_URL", "").strip()
        if not repository_url:
            raise ConfigurationError("REPOSITORY_URL is required")
        parsed_url = urlsplit(repository_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ConfigurationError("REPOSITORY_URL must use HTTPS")
        if parsed_url.query or parsed_url.fragment:
            raise ConfigurationError(
                "REPOSITORY_URL must not contain a query or fragment"
            )
        if parsed_url.username is not None or parsed_url.password is not None:
            raise ConfigurationError(
                "REPOSITORY_URL must not contain credentials; use GIT_USERNAME and GIT_TOKEN"
            )
        return cls(
            repository_url=repository_url,
            repository_branch=os.environ.get("REPOSITORY_BRANCH", "main"),
            octodns_config_file=os.environ.get(
                "OCTODNS_CONFIG_FILE", "config-internal.yaml"
            ),
            poll_interval_seconds=_positive_int("POLL_INTERVAL_SECONDS", 300),
            retry_interval_seconds=_positive_int("RETRY_INTERVAL_SECONDS", 60),
            command_timeout_seconds=_positive_int("COMMAND_TIMEOUT_SECONDS", 120),
            worktree=Path(os.environ.get("WORKTREE", "/data/repository")),
            state_file=Path(
                os.environ.get("STATE_FILE", "/data/last-applied-commit")
            ),
            run_once=os.environ.get("RUN_ONCE", "false").lower()
            in {"1", "true", "yes"},
        )


class CommandRunner:
    def __init__(self, timeout_seconds: int, git_allow_protocol: str = "https"):
        self.timeout_seconds = timeout_seconds
        self.git_allow_protocol = git_allow_protocol

    def run(
        self,
        command: list[str],
        cwd: Path | None = None,
        scope: str | None = None,
    ) -> str:
        LOG.info("running: %s", shlex.join(command))
        environment = os.environ.copy()
        if scope == "git":
            git_credentials = {
                name: environment[name]
                for name in ("GIT_TOKEN", "GIT_USERNAME")
                if name in environment
            }
            for name in tuple(environment):
                if name.startswith("OCTODNS_") or name.startswith("GIT_"):
                    environment.pop(name)
            environment.update(git_credentials)
            environment["GIT_CONFIG_NOSYSTEM"] = "1"
            environment["GIT_CONFIG_GLOBAL"] = os.devnull
            environment["GIT_CONFIG_COUNT"] = "1"
            environment["GIT_CONFIG_KEY_0"] = "core.hooksPath"
            environment["GIT_CONFIG_VALUE_0"] = os.devnull
            environment["GIT_ALLOW_PROTOCOL"] = self.git_allow_protocol
            environment["GIT_TERMINAL_PROMPT"] = "0"
            environment["GIT_ASKPASS"] = "/usr/local/bin/git-askpass"
        elif scope == "octodns":
            environment.pop("GIT_TOKEN", None)
            environment.pop("GIT_USERNAME", None)
        elif scope is not None:
            raise ValueError(f"unknown command scope: {scope}")
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except subprocess.CalledProcessError as error:
            if error.stdout:
                LOG.error("command output:\n%s", error.stdout.rstrip())
            raise
        if completed.stdout:
            LOG.info("%s", completed.stdout.rstrip())
        return completed.stdout


class GitRepository:
    def __init__(
        self,
        repository_url: str,
        branch: str,
        worktree: Path,
        config_file: str,
        runner,
    ):
        self.repository_url = repository_url
        self.branch = branch
        self.worktree = worktree
        config_path = Path(config_file)
        if config_path.is_absolute() or ".." in config_path.parts:
            raise ConfigurationError("octoDNS config path must stay inside checkout")
        self.config_file = config_file
        self.runner = runner

    def update(self) -> tuple[str, Path]:
        _reject_symlink_components(self.worktree, "repository worktree")
        if not (self.worktree / ".git").is_dir():
            if self.worktree.exists():
                raise ConfigurationError(
                    "repository worktree exists but is not a Git checkout"
                )
            self.worktree.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f".{self.worktree.name}.clone-",
                    dir=self.worktree.parent,
                )
            )
            try:
                self.runner.run(
                    [
                        "git",
                        "clone",
                        "--branch",
                        self.branch,
                        self.repository_url,
                        str(temporary),
                    ],
                    scope="git",
                )
                temporary.replace(self.worktree)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        else:
            origin_url = self.runner.run(
                ["git", "remote", "get-url", "origin"],
                cwd=self.worktree,
                scope="git",
            ).strip()
            if origin_url != self.repository_url:
                raise ConfigurationError(
                    f"repository origin mismatch: expected {self.repository_url}, got {origin_url}"
                )
            if self.runner.run(
                ["git", "status", "--porcelain"],
                cwd=self.worktree,
                scope="git",
            ).strip():
                raise ConfigurationError("repository worktree is not clean")
            self.runner.run(
                ["git", "fetch", "--prune", "origin", self.branch],
                cwd=self.worktree,
                scope="git",
            )
            self.runner.run(
                ["git", "checkout", self.branch],
                cwd=self.worktree,
                scope="git",
            )
            self.runner.run(
                ["git", "merge", "--ff-only", f"origin/{self.branch}"],
                cwd=self.worktree,
                scope="git",
            )
        commit = self.runner.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.worktree,
            scope="git",
        ).strip()
        config_path = self.worktree / self.config_file
        resolved_worktree = self.worktree.resolve()
        resolved_config = config_path.resolve()
        if not resolved_config.is_relative_to(resolved_worktree):
            raise ConfigurationError("octoDNS config path must stay inside checkout")
        for path in self.worktree.rglob("*"):
            relative = path.relative_to(self.worktree)
            if relative.parts[0] != ".git" and path.is_symlink():
                raise ConfigurationError(
                    f"repository symlinks are not allowed: {relative}"
                )
        if not config_path.is_file():
            raise ConfigurationError(f"octoDNS config file not found: {self.config_file}")
        return commit, config_path


class AppliedState:
    def __init__(self, path: Path):
        self.path = path
        self._validate_path()

    def _validate_path(self) -> None:
        _reject_symlink_components(self.path, "state file")

    def load(self) -> str | None:
        self._validate_path()
        if not self.path.exists():
            return None
        value = self.path.read_text(encoding="utf-8").strip()
        return value or None

    def save(self, commit: str) -> None:
        self._validate_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(f"{commit}\n", encoding="utf-8")
        temporary.replace(self.path)


class Reconciler:
    def __init__(self, repository, state, runner):
        self.repository = repository
        self.state = state
        self.runner = runner

    def reconcile_once(self) -> bool:
        commit, config_file = self.repository.update()
        if self.state.load() == commit:
            LOG.info("reconciling previously applied commit for drift")

        option = f"--config-file={config_file}"
        cwd = config_file.parent
        self.runner.run(
            ["octodns-validate", option, "--all"], cwd=cwd, scope="octodns"
        )
        self.runner.run(["octodns-sync", option], cwd=cwd, scope="octodns")
        self.runner.run(
            ["octodns-sync", option, "--doit"], cwd=cwd, scope="octodns"
        )
        verification = self.runner.run(
            ["octodns-sync", option], cwd=cwd, scope="octodns"
        )
        if "No changes were planned" not in verification:
            raise ReconciliationError(
                "post-apply verification still plans changes; commit not marked applied"
            )
        self.state.save(commit)
        return True


def run_service(
    reconciler,
    run_once: bool,
    poll_interval_seconds: int,
    retry_interval_seconds: int,
    sleep=time.sleep,
) -> None:
    while True:
        try:
            reconciler.reconcile_once()
        except Exception:
            LOG.exception("reconciliation failed")
            if run_once:
                raise
            sleep(retry_interval_seconds)
            continue
        if run_once:
            return
        sleep(poll_interval_seconds)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = Config.from_env()
    state = AppliedState(config.state_file)
    state.path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state.path.parent / "reconciler.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another reconciler process holds the lock") from error

        runner = CommandRunner(config.command_timeout_seconds)
        repository = GitRepository(
            repository_url=config.repository_url,
            branch=config.repository_branch,
            worktree=config.worktree,
            config_file=config.octodns_config_file,
            runner=runner,
        )
        reconciler = Reconciler(
            repository=repository,
            state=state,
            runner=runner,
        )
        run_service(
            reconciler,
            run_once=config.run_once,
            poll_interval_seconds=config.poll_interval_seconds,
            retry_interval_seconds=config.retry_interval_seconds,
        )


if __name__ == "__main__":
    main()
