"""Adopt a template into an existing, non-Copier project.

Unlike :func:`copier.run_copy`, which bootstraps a new project from a
template, :func:`copier.run_adopt` applies a template to an *existing*
project that was not originally created with Copier. It never destroys
content that already exists at the destination: new files are created,
identical files are left untouched, and conflicting files (same path,
different content) are written alongside the original as ``<file>.copier-new``
so the user can merge them manually.

An adopt operation follows a plan→apply→rollback lifecycle, tracked in a
crash journal so that an interrupted apply can be recovered.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pathspec import PathSpec

from ._main import Worker
from ._settings import Settings, SettingsModel
from ._tools import Style, printf, scantree
from ._types import AnyByStrDict, RelativePath
from .errors import UserMessageError

#: Version of the adopt journal format.
JOURNAL_VERSION = 1

#: Name of the crash-journal / metadata file written alongside the answers file.
JOURNAL_NAME = ".copier-adopt.json"

#: Folder where pre-adopt backups of conflicting files are stored.
BACKUP_DIR_NAME = ".copier-adopt-backup"


@dataclass
class JournalEntry:
    """A single record describing one file's adopt action."""

    relpath: str
    action: str
    backup_path: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class AdoptJournal:
    """The crash journal for an adopt operation."""

    version: int = JOURNAL_VERSION
    src_path: str = ""
    answers_file: str = ".copier-answers.yml"
    entries: list[JournalEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> AdoptJournal:
        """Load a journal from disk, returning an empty journal if absent."""
        if not path.exists():
            return cls()
        with path.open("r", encoding="utf-8") as fd:
            data = json.load(fd)
        entries = [
            JournalEntry(
                relpath=e["relpath"],
                action=e["action"],
                backup_path=e.get("backup_path"),
                timestamp=e.get("timestamp", ""),
            )
            for e in data.get("entries", [])
        ]
        return cls(
            version=data.get("version", JOURNAL_VERSION),
            src_path=data.get("src_path", ""),
            answers_file=data.get("answers_file", ".copier-answers.yml"),
            entries=entries,
        )

    def save(self, path: Path) -> None:
        """Persist the journal to disk as versioned JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "version": self.version,
            "src_path": self.src_path,
            "answers_file": self.answers_file,
            "entries": [
                {
                    "relpath": e.relpath,
                    "action": e.action,
                    "backup_path": e.backup_path,
                    "timestamp": e.timestamp,
                }
                for e in self.entries
            ],
        }
        with path.open("w", encoding="utf-8") as fd:
            json.dump(data, fd, indent=2, ensure_ascii=False)
            fd.write("\n")

    def applied_relpaths(self) -> set[str]:
        """Return the set of relative paths already recorded in the journal."""
        return {e.relpath for e in self.entries}


@dataclass
class AdoptAction:
    """Classification of a single rendered file during adopt."""

    relpath: str
    action: str  # "create" | "conflict" | "skip" | "skip-if-exists"
    content: bytes
    mode: int | None = None


def _walk_rendered(root: Path) -> list[tuple[Path, Path]]:
    """Walk a rendered template dir, returning (relpath, abspath) pairs."""
    result: list[tuple[Path, Path]] = []
    for entry in scantree(str(root), follow_symlinks=False):
        if entry.is_file(follow_symlinks=False):
            abspath = Path(entry.path)
            relpath = abspath.relative_to(root)
            result.append((relpath, abspath))
    return result


def _safe_render_skip_pattern(worker: Worker, pattern: str) -> str:
    """Render a skip-if-exists pattern, falling back to the raw string on error."""
    try:
        return worker._render_string(pattern)
    except Exception:  # noqa: BLE001
        return pattern


def _plan_actions(
    dst: Path,
    rendered_root: Path,
    skip_if_exists: Sequence[str],
    worker: Worker,
) -> list[AdoptAction]:
    """Classify every rendered file against the destination."""
    actions: list[AdoptAction] = []
    patterns: list[str] = [
        _safe_render_skip_pattern(worker, p) for p in worker.template.skip_if_exists
    ]
    patterns.extend(skip_if_exists)
    spec = PathSpec.from_lines("gitwildmatch", patterns) if patterns else None

    answers_relpath = str(worker.answers_relpath)

    for relpath, rendered_file in _walk_rendered(rendered_root):
        rel_str = relpath.as_posix()
        # Skip the answers file — it is written separately.
        if rel_str == answers_relpath:
            continue
        dst_abspath = dst / relpath

        if spec and spec.match_file(rel_str):
            action = "skip-if-exists"
        elif not dst_abspath.exists():
            action = "create"
        elif (
            dst_abspath.is_file()
            and dst_abspath.read_bytes() == rendered_file.read_bytes()
        ):
            action = "skip"
        else:
            action = "conflict"

        actions.append(
            AdoptAction(
                relpath=rel_str,
                action=action,
                content=rendered_file.read_bytes(),
                mode=rendered_file.stat().st_mode & 0o777,
            )
        )

    return actions


def _print_plan(actions: list[AdoptAction], quiet: bool = False) -> None:
    """Print the planned action table."""
    if quiet:
        return
    printf("PLAN", "Action  Path", style=Style.IGNORE, file_=sys.stderr)
    style_map = {
        "create": Style.OK,
        "conflict": Style.DANGER,
        "skip": Style.IGNORE,
        "skip-if-exists": Style.WARNING,
    }
    for act in actions:
        printf(
            act.action,
            act.relpath,
            style=style_map.get(act.action, Style.IGNORE),
            file_=sys.stderr,
        )
    n_conflicts = sum(1 for a in actions if a.action == "conflict")
    n_create = sum(1 for a in actions if a.action == "create")
    n_skip = sum(1 for a in actions if a.action == "skip")
    n_skip_if_exists = sum(1 for a in actions if a.action == "skip-if-exists")
    print(
        f"\n{n_create} file(s) to create, "
        f"{n_conflicts} conflict(s), "
        f"{n_skip} file(s) unchanged, "
        f"{n_skip_if_exists} to skip-if-exists.",
        file=sys.stderr,
    )


def _write_answers_file(
    dst: Path,
    worker: Worker,
    answers_file: Path | str | None,
) -> Path:
    """Write the copier answers file at the destination."""
    if answers_file is None:
        answers_relpath = Path(worker.answers_relpath)
    elif isinstance(answers_file, str):
        answers_relpath = RelativePath(answers_file)
    else:
        answers_relpath = answers_file
    dst_answers = dst / answers_relpath
    dst_answers.parent.mkdir(parents=True, exist_ok=True)
    to_remember = worker._answers_to_remember()
    content = yaml.safe_dump(
        dict(to_remember),
        sort_keys=False,
        allow_unicode=True,
        width=2147483647,
    )
    header = "# Changes here will be overwritten by Copier; NEVER EDIT MANUALLY\n"
    dst_answers.write_bytes((header + content).encode("utf-8"))
    return dst_answers


def _backup_file(src: Path, backup_dir: Path) -> str:
    """Copy *src* into *backup_dir*; return the relative backup path."""
    if not src.exists() and not src.is_symlink():
        return ""
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_name = f"{uuid4().hex[:8]}_{src.name}"
    backup_path = backup_dir / backup_name
    shutil.copy2(src, backup_path)
    return str(backup_path.relative_to(backup_dir))


def _apply_action(
    dst: Path,
    act: AdoptAction,
    backup_dir: Path,
    journal: AdoptJournal,
) -> None:
    """Apply a single adopt action, recording backups in the journal."""
    dst_abspath = dst / act.relpath

    if act.action in ("skip", "skip-if-exists"):
        return

    if act.action == "create":
        dst_abspath.parent.mkdir(parents=True, exist_ok=True)
        dst_abspath.write_bytes(act.content)
        if act.mode is not None:
            try:
                dst_abspath.chmod(act.mode)
            except (OSError, PermissionError):
                pass
        journal.entries.append(
            JournalEntry(relpath=act.relpath, action="create", backup_path=None)
        )
        return

    if act.action == "conflict":
        # Conservative: never overwrite the existing file. Write the
        # template version alongside as <file>.copier-new, after backing up
        # the original.
        backup_path = _backup_file(dst_abspath, backup_dir)
        new_file = dst_abspath.parent / f"{dst_abspath.name}.copier-new"
        dst_abspath.parent.mkdir(parents=True, exist_ok=True)
        new_file.write_bytes(act.content)
        if act.mode is not None:
            try:
                new_file.chmod(act.mode)
            except (OSError, PermissionError):
                pass
        journal.entries.append(
            JournalEntry(
                relpath=act.relpath, action="conflict", backup_path=backup_path
            )
        )


def _rollback(dst: Path, journal: AdoptJournal) -> None:
    """Restore pre-adopt state from journal + backups."""
    for entry in reversed(journal.entries):
        relpath = entry.relpath
        dst_file = dst / relpath
        if entry.action == "create":
            if dst_file.exists() or dst_file.is_symlink():
                dst_file.unlink()
        elif entry.action == "conflict":
            backup_abspath = dst / BACKUP_DIR_NAME / (entry.backup_path or "")
            if backup_abspath.exists():
                shutil.copy2(backup_abspath, dst_file)
            new_file = dst_file.parent / f"{dst_file.name}.copier-new"
            if new_file.exists():
                new_file.unlink()
    backup_dir = dst / BACKUP_DIR_NAME
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    (dst / JOURNAL_NAME).unlink(missing_ok=True)


def _render_template_to_temp(
    src_path: str,
    *,
    data: AnyByStrDict | None = None,
    answers_file: Path | str | None = None,
    vcs_ref: Any = None,
    settings: Settings | SettingsModel | None = None,
    exclude: Sequence[str] = (),
    use_prereleases: bool = False,
    skip_if_exists: Sequence[str] = (),
    defaults: bool = False,
    user_defaults: dict[str, Any] | None = None,
    unsafe: bool = False,
    skip_tasks: bool = True,
    ask: Sequence[str] = (),
) -> tuple[Worker, Path, Any]:
    """Render a template into a temporary directory using a Copier Worker.

    Returns the worker (with answers populated), the temp directory path,
    and the TemporaryDirectory context manager (to be kept alive by the
    caller).
    """
    from tempfile import TemporaryDirectory

    tmpdir_ctx = TemporaryDirectory(prefix="copier-adopt-render-")
    tmpdir = Path(tmpdir_ctx.name)

    worker = Worker(
        src_path=src_path,
        dst_path=tmpdir,
        data=data or {},
        answers_file=(
            RelativePath(answers_file)
            if isinstance(answers_file, str)
            else answers_file
        ),
        vcs_ref=vcs_ref,
        settings=(
            SettingsModel(defaults=settings.defaults, trust=settings.trust)
            if isinstance(settings, Settings)
            else (settings or SettingsModel.from_file())
        ),
        exclude=exclude,
        use_prereleases=use_prereleases,
        skip_if_exists=skip_if_exists,
        defaults=defaults,
        user_defaults=user_defaults or {},
        overwrite=False,
        pretend=False,
        quiet=True,
        unsafe=unsafe,
        skip_tasks=skip_tasks,
        ask=ask,
    )
    worker.__enter__()
    # Actually render the template into the temp directory.
    worker.run_copy()

    return worker, tmpdir, tmpdir_ctx


def _apply_phase(
    dst: Path,
    src_path: str,
    worker: Worker,
    rendered_root: Path,
    answers_file: Path | str | None,
    answers_relpath: Path,
    answers_file_abspath: Path,
    journal_path: Path,
    skip_if_exists: Sequence[str],
    recover: bool,
    quiet: bool,
) -> None:
    """Compute actions, apply them, and write the answers file + journal."""
    if recover:
        journal = AdoptJournal.load(journal_path)
        if not journal.entries and not journal_path.exists():
            raise UserMessageError(
                "No adopt journal found; cannot recover. Nothing to resume."
            )
        if not quiet:
            print(
                f"Recovering adopt from journal "
                f"({len(journal.entries)} recorded entries)...",
                file=sys.stderr,
            )
    else:
        journal = AdoptJournal(
            src_path=src_path,
            answers_file=str(answers_relpath),
        )

    actions = _plan_actions(dst, rendered_root, skip_if_exists, worker)
    applied_relpaths = journal.applied_relpaths()

    if not quiet:
        print(f"Applying template to {dst}...", file=sys.stderr)

    backup_dir = dst / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    for act in actions:
        if act.relpath in applied_relpaths:
            continue
        _apply_action(dst, act, backup_dir, journal)
        if not quiet:
            printf(act.action, act.relpath, file_=sys.stderr)

    answers_rel = str(answers_relpath)
    if answers_rel not in applied_relpaths:
        _write_answers_file(dst, worker, answers_file)
        journal.entries.append(
            JournalEntry(relpath=answers_rel, action="create", backup_path=None)
        )
    journal.save(journal_path)

    if not quiet:
        print(
            f"\nAdopt complete. Journal: {journal_path}, "
            f"Answers: {answers_file_abspath}",
            file=sys.stderr,
        )


def run_adopt(  # noqa: C901
    src_path: str,
    dst_path: Path | str = ".",
    data: dict[str, Any] | None = None,
    *,
    answers_file: Path | str | None = None,
    vcs_ref: str | Any = None,
    settings: Settings | SettingsModel | None = None,
    exclude: Sequence[str] = (),
    use_prereleases: bool = False,
    skip_if_exists: Sequence[str] = (),
    defaults: bool = False,
    user_defaults: dict[str, Any] | None = None,
    unsafe: bool = False,
    skip_tasks: bool = True,
    ask: Sequence[str] = (),
    pretend: bool = False,
    quiet: bool = False,
    plan: bool = False,
    recover: bool = False,
    rollback: bool = False,
) -> Worker:
    """Adopt a template into an existing, non-Copier project.

    Unlike :func:`copier.run_copy`, this never overwrites existing files.
    Instead it:

    - Creates new files from the template.
    - Skips files that already exist and are identical.
    - Writes conflicting files alongside the original as
      ``<file>.copier-new``.

    A crash journal (``.copier-adopt.json``) and backups
    (``.copier-adopt-backup/``) are maintained so an interrupted apply can be
    resumed with ``--recover`` or rolled back with ``--rollback``.

    Args:
        src_path:
            Template source (local path or URL).
        dst_path:
            Destination path — an existing, non-Copier project.
        data:
            Extra context data to pass to the template.
        answers_file:
            Path to the answers file. Defaults to ``.copier-answers.yml``.
        vcs_ref:
            Version control reference (branch, tag, commit) to use from the
            template repository.
        settings:
            Custom settings to override the user config / defaults.
        exclude:
            Paths to exclude from rendering.
        use_prereleases:
            Allow using pre-release versions of the template when fetching
            from a repository.
        skip_if_exists:
            Extra ``skip-if-exists`` patterns (in addition to those declared
            in the template).
        defaults:
            Use default answers to all questions.
        user_defaults:
            User-level defaults to merge into the template context.
        unsafe:
            Allow running unsafe template code (e.g. ``_tasks``).
        skip_tasks:
            Skip running template tasks. Defaults to ``True``.
        ask:
            Extra questions to ask, even if they would otherwise be skipped.
        plan:
            Only print the planned actions; write nothing.
        recover:
            Resume an interrupted adopt from the crash journal.
        rollback:
            Restore the pre-adopt state from the journal + backups.
        pretend:
            Behave as if ``--plan`` was given (no writes).
        quiet:
            Suppress informational output.

    Returns:
        The :class:`~copier._main.Worker` that rendered the template.
    """
    dst = Path(dst_path).resolve()

    # --- rollback ---
    if rollback:
        journal_path = dst / JOURNAL_NAME
        if not journal_path.exists():
            raise UserMessageError("No adopt journal found; nothing to roll back.")
        journal = AdoptJournal.load(journal_path)
        if not quiet:
            print("Rolling back adopt changes...", file=sys.stderr)
        _rollback(dst, journal)
        if not quiet:
            print("Rollback complete.", file=sys.stderr)
        return Worker(src_path=src_path, dst_path=dst, defaults=True, quiet=True)

    # --- validate destination ---
    if not dst.exists():
        raise UserMessageError(
            f"Destination {dst} does not exist. Adopt requires an existing project."
        )
    if not dst.is_dir():
        raise UserMessageError(f"Destination {dst} is not a directory.")

    answers_relpath = (
        Path(answers_file) if answers_file else Path(".copier-answers.yml")
    )
    answers_file_abspath = dst / answers_relpath
    journal_path = dst / JOURNAL_NAME

    # --- detect existing copier project ---
    if answers_file_abspath.exists() and not recover:
        raise UserMessageError(
            f"{dst} appears to be already managed by Copier "
            f"({answers_relpath} exists). Use `copier update` instead, "
            f"or pass `--recover`/`--rollback` to manage the adopt state."
        )

    # --- check destination is non-empty ---
    if not any(dst.iterdir()):
        raise UserMessageError(
            f"Destination {dst} is empty. Adopt is for existing projects; "
            f"use `copier copy` to bootstrap a new project instead."
        )

    # --- render template into temp ---
    tmpdir_ctx = None
    worker = None
    try:
        worker, tmpdir, tmpdir_ctx = _render_template_to_temp(
            src_path=src_path,
            data=data or {},
            answers_file=answers_file,
            vcs_ref=vcs_ref,
            settings=settings,
            exclude=exclude,
            use_prereleases=use_prereleases,
            skip_if_exists=skip_if_exists,
            defaults=defaults,
            user_defaults=user_defaults or {},
            unsafe=unsafe,
            skip_tasks=skip_tasks,
            ask=ask,
        )
        rendered_root = tmpdir

        # --- plan mode ---
        if plan or pretend:
            actions = _plan_actions(dst, rendered_root, skip_if_exists, worker)
            _print_plan(actions, quiet=quiet)
            return worker

        # --- apply phase (normal or recover) ---
        _apply_phase(
            dst=dst,
            src_path=src_path,
            worker=worker,
            rendered_root=rendered_root,
            answers_file=answers_file,
            answers_relpath=answers_relpath,
            answers_file_abspath=answers_file_abspath,
            journal_path=journal_path,
            skip_if_exists=skip_if_exists,
            recover=recover,
            quiet=quiet,
        )
    finally:
        # Always exit the worker / temp dir.
        if worker is not None:
            worker.__exit__(None, None, None)
        if tmpdir_ctx is not None:
            tmpdir_ctx.cleanup()

    return worker
