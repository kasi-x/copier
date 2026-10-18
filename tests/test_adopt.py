from __future__ import annotations

import json
from pathlib import Path

import pytest

import copier
from copier._adopt import (
    BACKUP_DIR_NAME,
    JOURNAL_NAME,
    AdoptJournal,
)
from copier._cli import CopierApp
from copier._user_data import load_answersfile_data
from copier.errors import UserMessageError

from .helpers import build_file_tree

# A reusable template used across most adopt tests.
ADOPT_TEMPLATE_TREE: dict[str, str] = {
    "copier.yml": "name: world\n",
    "hello.txt.jinja": "Hello, {{ name }}!\n",
    "new_file.txt": "brand new\n",
    "existing.txt": "template-version\n",
    "{{ _copier_conf.answers_file }}.jinja": "{{ _copier_answers|to_nice_yaml }}\n",
}


def _build_template(root: Path) -> Path:
    """Build the adopt test template at *root* and return root."""
    build_file_tree({(root / k): v for k, v in ADOPT_TEMPLATE_TREE.items()})
    return root


def _build_dst(tmp_path: Path) -> Path:
    """Build an existing (non-copier) project directory and return it."""
    dst = tmp_path / "project"
    dst.mkdir(parents=True)
    (dst / "existing.txt").write_text("original-content\n")
    (dst / "conflict_file.txt").write_text("original-conflict\n")
    return dst


# --- Python API tests ---


def test_adopt_creates_new_files(tmp_path: Path) -> None:
    """New files from the template are created in the destination."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    # new_file.txt exists in template, not in dst → should be created
    copier.run_adopt(str(src), dst, defaults=True)
    assert (dst / "new_file.txt").read_text() == "brand new\n"
    assert (dst / "hello.txt").read_text() == "Hello, world!\n"


def test_adopt_does_not_overwrite_conflicts(tmp_path: Path) -> None:
    """Conflicting files are written as <file>.copier-new; originals preserved."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    copier.run_adopt(str(src), dst, defaults=True)
    # original is untouched
    assert (dst / "existing.txt").read_text() == "original-content\n"
    # template version written alongside
    assert (dst / "existing.txt.copier-new").read_text() == "template-version\n"


def test_adopt_writes_answers_file(tmp_path: Path) -> None:
    """The answers file is written so future `copier update` works."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    copier.run_adopt(str(src), dst, defaults=True)
    answers = load_answersfile_data(dst)
    assert answers["_src_path"] == str(src)
    assert answers["name"] == "world"


def test_adopt_writes_journal(tmp_path: Path) -> None:
    """A versioned journal file is written with per-path actions."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    copier.run_adopt(str(src), dst, defaults=True)
    journal_path = dst / JOURNAL_NAME
    assert journal_path.exists()
    journal = AdoptJournal.load(journal_path)
    assert journal.version == 1
    assert journal.src_path == str(src)
    actions = {e.relpath: e.action for e in journal.entries}
    assert actions["hello.txt"] == "create"
    assert actions["existing.txt"] == "conflict"
    assert actions[".copier-answers.yml"] == "create"


def test_adopt_plan_does_not_write(tmp_path: Path) -> None:
    """`--plan` prints actions without writing any files."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    copier.run_adopt(str(src), dst, defaults=True, plan=True, quiet=True)
    assert not (dst / "hello.txt").exists()
    assert not (dst / "existing.txt.copier-new").exists()
    assert not (dst / ".copier-answers.yml").exists()
    assert not (dst / JOURNAL_NAME).exists()


def test_adopt_pretend_does_not_write(tmp_path: Path) -> None:
    """`pretend=True` behaves like `plan=True`: no writes."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    copier.run_adopt(str(src), dst, defaults=True, pretend=True, quiet=True)
    assert not (dst / "hello.txt").exists()
    assert not (dst / JOURNAL_NAME).exists()


def test_adopt_idempotent(tmp_path: Path) -> None:
    """Re-running adopt with --recover doesn't duplicate conflict files."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    copier.run_adopt(str(src), dst, defaults=True)
    first_journal = json.loads((dst / JOURNAL_NAME).read_text())

    # Recover re-applies — entries already in journal are skipped.
    copier.run_adopt(str(src), dst, defaults=True, recover=True)
    second_journal = json.loads((dst / JOURNAL_NAME).read_text())

    # No duplicate .copier-new files
    new_files = list(dst.glob("*.copier-new"))
    assert len(new_files) == 1  # existing.txt conflict
    # Journal entries should not grow
    assert len(second_journal["entries"]) == len(first_journal["entries"])


def test_adopt_recover_skips_applied(tmp_path: Path) -> None:
    """Recover skips entries already in the journal."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)

    # Initial apply
    copier.run_adopt(str(src), dst, defaults=True)

    # Now manually create a new file in the template that wasn't there before,
    # and re-run with recover. Since the journal already has all entries,
    # nothing new should be written.
    # We simulate a crash by removing one applied file and its journal entry,
    # then recovering.
    (dst / "hello.txt").unlink()
    # Remove hello.txt entry from journal
    journal = AdoptJournal.load(dst / JOURNAL_NAME)
    journal.entries = [e for e in journal.entries if e.relpath != "hello.txt"]
    journal.save(dst / JOURNAL_NAME)

    # Recover should re-create hello.txt
    copier.run_adopt(str(src), dst, defaults=True, recover=True)
    assert (dst / "hello.txt").read_text() == "Hello, world!\n"


def test_adopt_rollback_restores_state(tmp_path: Path) -> None:
    """Rollback removes created files, restores conflicts, deletes journal + backups."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    copier.run_adopt(str(src), dst, defaults=True)
    assert (dst / "hello.txt").exists()
    assert (dst / "existing.txt.copier-new").exists()
    assert (dst / JOURNAL_NAME).exists()
    assert (dst / BACKUP_DIR_NAME).exists()

    copier.run_adopt(str(src), dst, defaults=True, rollback=True)

    # Created file removed
    assert not (dst / "hello.txt").exists()
    # Conflict restored to original
    assert (dst / "existing.txt").read_text() == "original-content\n"
    # .copier-new removed
    assert not (dst / "existing.txt.copier-new").exists()
    # Journal + backup dir removed
    assert not (dst / JOURNAL_NAME).exists()
    assert not (dst / BACKUP_DIR_NAME).exists()


def test_adopt_rollback_without_journal_raises(tmp_path: Path) -> None:
    """Rolling back without a journal raises an error."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    with pytest.raises(UserMessageError, match="No adopt journal found"):
        copier.run_adopt(str(src), dst, defaults=True, rollback=True)


def test_adopt_empty_dst_raises(tmp_path: Path) -> None:
    """Adopting an empty destination raises."""
    src = _build_template(tmp_path / "template")
    dst = tmp_path / "empty"
    dst.mkdir()
    with pytest.raises(UserMessageError, match="empty"):
        copier.run_adopt(str(src), dst, defaults=True)


def test_adopt_nonexistent_dst_raises(tmp_path: Path) -> None:
    """Adopting a nonexistent destination raises."""
    src = _build_template(tmp_path / "template")
    dst = tmp_path / "nonexistent"
    with pytest.raises(UserMessageError, match="does not exist"):
        copier.run_adopt(str(src), dst, defaults=True)


def test_adopt_already_copier_project_raises(tmp_path: Path) -> None:
    """Adopting an existing copier project (answers file present) raises."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    (dst / ".copier-answers.yml").write_text("_src_path: something\n")
    with pytest.raises(UserMessageError, match="already managed by Copier"):
        copier.run_adopt(str(src), dst, defaults=True)


def test_adopt_skip_if_exists(tmp_path: Path) -> None:
    """Files matching --skip-if-exists are classified as skip-if-exists."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    # existing.txt is a conflict, but if we skip it, no .copier-new is written
    copier.run_adopt(str(src), dst, defaults=True, skip_if_exists=["existing.txt"])
    assert (dst / "existing.txt").read_text() == "original-content\n"
    assert not (dst / "existing.txt.copier-new").exists()


def test_adopt_exclude(tmp_path: Path) -> None:
    """Excluded files are not adopted."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    copier.run_adopt(
        str(src), dst, defaults=True, exclude=["hello.txt"]
    )
    assert not (dst / "hello.txt").exists()


def test_adopt_skip_if_exists_in_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """skip-if-exists files are shown as skip-if-exists in plan output."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    copier.run_adopt(
        str(src), dst, defaults=True, plan=True,
        skip_if_exists=["existing.txt"], quiet=False
    )
    captured = capsys.readouterr()
    # Should mention skip-if-exists (output goes to stderr)
    assert "skip-if-exists" in captured.err or "skip-if-exists" in captured.out


# --- CLI tests ---


def test_cli_adopt_plan(tmp_path: Path) -> None:
    """`copier adopt --plan` prints the action table, exit 0."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    run_result = CopierApp.run(
        ["copier", "adopt", "--plan", "--defaults", "--quiet", str(src), str(dst)],
        exit=False,
    )
    assert run_result[1] == 0
    assert not (dst / "hello.txt").exists()


def test_cli_adopt_apply(tmp_path: Path) -> None:
    """`copier adopt` creates new files and conflict sidecars."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    run_result = CopierApp.run(
        ["copier", "adopt", "--defaults", "--quiet", str(src), str(dst)],
        exit=False,
    )
    assert run_result[1] == 0
    assert (dst / "hello.txt").read_text() == "Hello, world!\n"
    assert (dst / "existing.txt.copier-new").read_text() == "template-version\n"
    assert (dst / JOURNAL_NAME).exists()


def test_cli_adopt_recover(tmp_path: Path) -> None:
    """`copier adopt --recover` resumes from the journal."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    CopierApp.run(
        ["copier", "adopt", "--defaults", "--quiet", str(src), str(dst)],
        exit=False,
    )
    # Remove one applied file to simulate a crash
    (dst / "hello.txt").unlink()
    journal = AdoptJournal.load(dst / JOURNAL_NAME)
    journal.entries = [e for e in journal.entries if e.relpath != "hello.txt"]
    journal.save(dst / JOURNAL_NAME)

    run_result = CopierApp.run(
        ["copier", "adopt", "--recover", "--defaults", "--quiet", str(src), str(dst)],
        exit=False,
    )
    assert run_result[1] == 0
    assert (dst / "hello.txt").read_text() == "Hello, world!\n"


def test_cli_adopt_rollback(tmp_path: Path) -> None:
    """`copier adopt --rollback` restores pre-adopt state."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    CopierApp.run(
        ["copier", "adopt", "--defaults", "--quiet", str(src), str(dst)],
        exit=False,
    )
    assert (dst / "hello.txt").exists()

    run_result = CopierApp.run(
        ["copier", "adopt", "--rollback", "--quiet", str(src), str(dst)],
        exit=False,
    )
    assert run_result[1] == 0
    assert not (dst / "hello.txt").exists()
    assert not (dst / JOURNAL_NAME).exists()


def test_cli_adopt_force_defaults(tmp_path: Path) -> None:
    """`--force` is equivalent to `--defaults` for adopt."""
    src = _build_template(tmp_path / "template")
    dst = _build_dst(tmp_path)
    run_result = CopierApp.run(
        ["copier", "adopt", "--force", "--quiet", str(src), str(dst)],
        exit=False,
    )
    assert run_result[1] == 0
    assert (dst / "hello.txt").exists()
