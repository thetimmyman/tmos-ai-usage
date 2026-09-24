"""Private, local-only onboarding for TMOS AI Usage."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile


SCHEMA_VERSION = 1
MAX_SETUP_BYTES = 64 * 1024
REQUIRED_PI_FILES = ("index.js", "observer.js", "write_event.py", "package.json")
MAX_PI_FILE_BYTES = 2 * 1024 * 1024


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _state_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("state directory is required")
    return Path(value).expanduser().absolute()


def _components(path: Path):
    """Yield absolute path components from root, without resolving symlinks."""
    absolute = Path(os.path.abspath(path))
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        yield cursor


def _check_existing_components(path: Path, *, target_must_be_owned: bool = False) -> None:
    uid = os.geteuid()
    components = list(_components(path))
    for index, component in enumerate(components):
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("path contains a symlink component")
        if index == len(components) - 1 and target_must_be_owned:
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
                raise ValueError("target directory is not owned by the current user")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("path ancestor is not a directory")
        if stat.S_ISDIR(info.st_mode):
            # System ancestors such as / and /home may be root-owned, but they
            # must not be writable by other users. Sticky directories like /tmp
            # are safe for creation of a private child.
            writable = info.st_mode & 0o022
            sticky = info.st_mode & stat.S_ISVTX
            if writable and not sticky:
                raise ValueError("path ancestor has unsafe ownership or permissions")


def _private_dir(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    _check_existing_components(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("private directory must be an owned real directory")
    os.chmod(path, 0o700)
    return path


def _read_setup(state_dir: Path) -> dict | None:
    path = state_dir / "setup.json"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > MAX_SETUP_BYTES):
            raise ValueError("setup state must be a private owned regular file")
        raw = stream.read(MAX_SETUP_BYTES + 1)
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or not isinstance(value.get("initialized"), bool)
            or not isinstance(value.get("completed"), bool)):
        raise ValueError("setup state is invalid")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    raw = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".setup-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _task_destination() -> Path:
    return Path.home() / ".local" / "bin" / "tmos-ai-task"


def _pi_agent_dir() -> tuple[Path, str]:
    configured = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate, "using configured absolute Pi agent directory"
        return Path.home() / ".pi" / "agent", "PI_CODING_AGENT_DIR is not absolute; using the default Pi agent directory"
    return Path.home() / ".pi" / "agent", "using the default Pi agent directory"


def _pi_destination() -> tuple[Path, str]:
    agent, reason = _pi_agent_dir()
    return agent / "extensions" / "tmos-ai-usage", reason


def _symlink_matches(path: Path, expected: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISLNK(info.st_mode):
        return False
    link = os.readlink(path)
    actual = (path.parent / link).resolve() if not os.path.isabs(link) else Path(link).resolve()
    return actual == expected.resolve()


def _pi_tree_identical(destination: Path, source: Path) -> bool:
    try:
        info = os.lstat(destination)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return False
    if info.st_uid != os.geteuid():
        raise ValueError("existing Pi extension directory is not owned by the current user")
    for name in REQUIRED_PI_FILES:
        expected_path = source / name
        actual_path = destination / name
        expected = _read_regular_bounded(expected_path, MAX_PI_FILE_BYTES, owned=False)
        try:
            actual = _read_regular_bounded(actual_path, MAX_PI_FILE_BYTES, owned=True)
        except (FileNotFoundError, ValueError, OSError):
            return False
        if actual != expected:
            return False
    # Ignore unreferenced files. The manifest names index.js as the entrypoint;
    # unrelated existing files are left untouched and never read during install.
    return True


def _read_regular_bounded(path: Path, limit: int, *, owned: bool) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or (owned and info.st_uid != os.geteuid())
                or info.st_size > limit):
            raise ValueError("file is nonregular, unowned, or over the size limit")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file exceeds the size limit")
    return raw


def _safe_install_parent(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    _check_existing_components(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _check_existing_components(path, target_must_be_owned=True)
    return path


def _status(state_dir: Path, message: str = "") -> dict:
    root = _plugin_root()
    reports_dir = state_dir / "imports"
    outcome_dir = state_dir / "outcome-events"
    if os.path.lexists(state_dir):
        _check_existing_components(state_dir, target_must_be_owned=True)
        info = os.lstat(state_dir)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("state directory must be private and owned")
        setup = _read_setup(state_dir)
    else:
        _check_existing_components(state_dir)
        setup = None
    initialized = bool(setup and setup.get("initialized"))
    completed = bool(setup and setup.get("completed"))

    task_source = root / "bin" / "tmos-ai-task"
    task_available = (task_source.is_file() and not task_source.is_symlink()
                      and bool(task_source.stat().st_mode & 0o111)
                      and shutil.which("python3") is not None
                      and shutil.which("bash") is not None
                      and shutil.which("readlink") is not None)
    task_dest = _task_destination()
    _check_existing_components(task_dest.parent, target_must_be_owned=True)
    task_installed = _symlink_matches(task_dest, task_source)
    if task_installed:
        task_reason = "installed"
    elif os.path.lexists(task_dest):
        task_reason = "conflict: destination exists and was left unchanged"
    elif not task_available:
        task_reason = "bundled task command or a required shell utility is unavailable"
    else:
        task_reason = "not installed"

    pi_source = root / "integration" / "pi"
    pi_files_ok = all((pi_source / name).is_file() and not (pi_source / name).is_symlink()
                      for name in REQUIRED_PI_FILES)
    pi_executable = shutil.which("pi")
    pi_available = pi_files_ok and pi_executable is not None
    pi_dest, pi_location_reason = _pi_destination()
    _check_existing_components(pi_dest.parent, target_must_be_owned=True)
    pi_installed = _symlink_matches(pi_dest, pi_source)
    if not pi_installed and os.path.lexists(pi_dest) and pi_dest.is_dir() and not pi_dest.is_symlink():
        pi_installed = _pi_tree_identical(pi_dest, pi_source)
    if pi_installed:
        pi_reason = "installed; start a new Pi session or /reload to load the extension"
    elif os.path.lexists(pi_dest):
        pi_reason = "conflict: destination exists and was left unchanged"
    elif not pi_files_ok:
        pi_reason = "bundled Pi observer files are unavailable"
    elif not pi_executable:
        pi_reason = "Pi CLI is not on PATH; install the observer later or install Pi first"
    else:
        pi_reason = "not installed; " + pi_location_reason
    return {
        "schema_version": SCHEMA_VERSION,
        "initialized": initialized,
        "completed": completed,
        "task_command": {"installed": task_installed, "available": bool(task_available), "reason": task_reason},
        "pi_observer": {"installed": pi_installed, "available": bool(pi_available), "reason": pi_reason},
        "state_dir": str(state_dir),
        "reports_dir": str(reports_dir),
        "outcome_dir": str(outcome_dir),
        "message": message,
    }


def _initialize(state_dir: Path) -> str:
    state_dir = _private_dir(state_dir)
    reports_dir = _private_dir(state_dir / "imports")
    outcome_dir = _private_dir(state_dir / "outcome-events")
    existing = _read_setup(state_dir)
    if existing is None:
        existing = {"schema_version": SCHEMA_VERSION, "initialized": True,
                    "completed": False, "initialized_at": _stamp()}
    else:
        existing["initialized"] = True
        existing.setdefault("initialized_at", _stamp())
    _atomic_json(state_dir / "setup.json", existing)
    return f"Private state and incoming directories are ready: {reports_dir} and {outcome_dir}."


def _install_task_command() -> str:
    root = _plugin_root()
    source = root / "bin" / "tmos-ai-task"
    if not source.is_file() or source.is_symlink() or not source.stat().st_mode & 0o111:
        raise ValueError("bundled task command is unavailable")
    destination = _task_destination()
    parent = _safe_install_parent(destination.parent)
    destination = parent / destination.name
    if _symlink_matches(destination, source):
        return "Task command symlink is already installed."
    if os.path.lexists(destination):
        return "Conflict: task command destination exists; it was left unchanged."
    os.symlink(str(source.resolve()), destination)
    return "Task command installed."


def _install_pi_observer() -> str:
    root = _plugin_root()
    source = root / "integration" / "pi"
    if not source.is_dir() or not all((source / name).is_file() for name in REQUIRED_PI_FILES):
        raise ValueError("bundled Pi observer is unavailable")
    destination, location_reason = _pi_destination()
    parent = _safe_install_parent(destination.parent)
    destination = parent / destination.name
    if _symlink_matches(destination, source):
        return "Pi observer symlink is already installed; start a new session or /reload."
    if os.path.lexists(destination):
        if destination.is_dir() and not destination.is_symlink() and _pi_tree_identical(destination, source):
            return "Identical Pi observer files are already installed; start a new session or /reload."
        return "Conflict: Pi extension destination exists and differs; it was left unchanged."
    os.symlink(str(source.resolve()), destination, target_is_directory=True)
    return f"Pi observer installed ({location_reason}); start a new session or /reload."


def _remove_managed_symlink(destination: Path, source: Path, label: str) -> str:
    """Remove only our exact, user-owned symlink; preserve copied trees and conflicts."""
    _check_existing_components(destination.parent, target_must_be_owned=True)
    try:
        info = os.lstat(destination)
    except FileNotFoundError:
        return f"{label} is not installed."
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        return f"Conflict: {label} is not an owned managed symlink; it was left unchanged."
    if not _symlink_matches(destination, source):
        return f"Conflict: {label} points elsewhere; it was left unchanged."
    os.unlink(destination)
    return f"Removed the managed {label} symlink."


def _remove_task_command() -> str:
    return _remove_managed_symlink(
        _task_destination(), _plugin_root() / "bin" / "tmos-ai-task", "task command"
    )


def _remove_pi_observer() -> str:
    destination, _ = _pi_destination()
    return _remove_managed_symlink(
        destination, _plugin_root() / "integration" / "pi", "Pi observer"
    )


def _finish(state_dir: Path) -> str:
    setup = _read_setup(state_dir)
    if setup is None or not setup.get("initialized"):
        raise ValueError("initialize the private state directory before finishing setup")
    if not setup.get("completed"):
        setup["completed"] = True
        setup["completed_at"] = _stamp()
        _atomic_json(state_dir / "setup.json", setup)
    return "Setup is marked complete; this does not mark any usage or task evidence complete."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "initialize", "install-task-command",
                                            "install-pi-observer", "remove-task-command",
                                            "remove-pi-observer", "finish"))
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args(argv)
    state_dir = _state_path(args.state_dir)
    try:
        if args.action == "status":
            message = "Setup status loaded."
        elif args.action == "initialize":
            message = _initialize(state_dir)
        elif args.action == "install-task-command":
            message = _install_task_command()
        elif args.action == "install-pi-observer":
            message = _install_pi_observer()
        elif args.action == "remove-task-command":
            message = _remove_task_command()
        elif args.action == "remove-pi-observer":
            message = _remove_pi_observer()
        else:
            message = _finish(state_dir)
        print(json.dumps(_status(state_dir, message), sort_keys=True, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        # Keep errors actionable but never include a path, file contents, or
        # arbitrary exception text in the UI channel.
        if isinstance(exc, ValueError):
            safe_message = str(exc)
        elif isinstance(exc, (TypeError, json.JSONDecodeError)):
            safe_message = "Setup data is invalid; inspect the private setup state and retry."
        else:
            safe_message = "A local setup file or directory could not be accessed safely."
        print(json.dumps({"error": safe_message}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
