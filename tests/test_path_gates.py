from __future__ import annotations

from pathlib import Path

import pytest

from copier._main import run_copy

from .helpers import build_file_tree


@pytest.fixture
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": "feature_a: {type: bool, default: false}\n"
            "feature_b: {type: bool, default: false}\n",
            src / "keep.txt": "keep me\n",
            src / "keep.txt.gate": "{{ feature_a }}",
            src / "drop.txt": "drop me\n",
            src / "drop.txt.gate": "{{ feature_b }}",
            src / "always.txt": "always\n",
            # A directory gated by a `.gate` dotfile inside it.
            src / "sub" / "file.txt": "sub file\n",
            src / "sub" / ".gate": "{{ feature_a }}",
            # A directory gated by a sibling `.gate` file.
            src / "other" / "file.txt": "other file\n",
            src / "other.gate": "{{ feature_a }}",
        }
    )
    return src


def _dst(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("dst")


def test_gate_keeps_and_drops_files(template: Path, tmp_path: Path) -> None:
    """A falsy gate drops a file; a truthy one keeps it."""
    run_copy(
        str(template),
        tmp_path,
        data={"feature_a": False, "feature_b": True},
        quiet=True,
    )
    assert not (tmp_path / "keep.txt").exists()
    assert (tmp_path / "drop.txt").exists()
    assert (tmp_path / "always.txt").exists()
    assert not (tmp_path / "keep.txt.gate").exists()
    assert not (tmp_path / "drop.txt.gate").exists()


def test_gate_combos(template: Path, tmp_path: Path) -> None:
    """Files and directories are excluded according to their gate markers."""
    # feature_a on, feature_b off
    dst = tmp_path / "a_on"
    run_copy(
        str(template),
        dst,
        data={"feature_a": True, "feature_b": False},
        quiet=True,
    )
    assert (dst / "keep.txt").exists()
    assert not (dst / "drop.txt").exists()
    assert (dst / "sub" / "file.txt").exists()
    assert (dst / "other" / "file.txt").exists()
    assert not (dst / "sub" / ".gate").exists()
    assert not (dst / "other.gate").exists()

    # feature_a off, feature_b on
    dst = tmp_path / "b_on"
    run_copy(
        str(template),
        dst,
        data={"feature_a": False, "feature_b": True},
        quiet=True,
    )
    assert not (dst / "keep.txt").exists()
    assert (dst / "drop.txt").exists()
    assert not (dst / "sub").exists()
    assert not (dst / "other").exists()
    assert not (dst / "sub" / ".gate").exists()
    assert not (dst / "other.gate").exists()


def test_gate_never_copied(template: Path, tmp_path: Path) -> None:
    """`.gate` marker files are never written to the destination."""
    run_copy(
        str(template),
        tmp_path,
        data={"feature_a": True, "feature_b": True},
        quiet=True,
    )
    assert not list(tmp_path.rglob("*.gate"))
    assert (tmp_path / "keep.txt").exists()
    assert (tmp_path / "drop.txt").exists()
    assert (tmp_path / "sub" / "file.txt").exists()
    assert (tmp_path / "other" / "file.txt").exists()


def test_gate_comment_and_whitespace(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Comment lines and blank lines in a `.gate` file are ignored."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": "use_it: {type: bool, default: false}\n",
            src / "gated.txt": "gated\n",
            src
            / "gated.txt.gate": "# This gates the file\n\n{{ use_it }}\n# trailing comment\n",
        }
    )

    truthy_dst = tmp_path_factory.mktemp("dst_truthy")
    run_copy(
        str(src),
        truthy_dst,
        data={"use_it": True},
        quiet=True,
    )
    assert (truthy_dst / "gated.txt").exists()
    assert not (truthy_dst / "gated.txt.gate").exists()

    falsy_dst = tmp_path_factory.mktemp("dst_falsy")
    run_copy(
        str(src),
        falsy_dst,
        data={"use_it": False},
        quiet=True,
    )
    assert not (falsy_dst / "gated.txt").exists()
    assert not (falsy_dst / "gated.txt.gate").exists()


def test_gate_bad_jinja_raises(template: Path, tmp_path: Path) -> None:
    """A Jinja error in a `.gate` file raises a UserMessageError naming it."""
    bad_gate = template / "keep.txt.gate"
    bad_gate.write_text("{{ feature_a ", encoding="utf-8")
    from copier.errors import UserMessageError

    with pytest.raises(UserMessageError) as exc_info:
        run_copy(
            str(template),
            tmp_path,
            data={"feature_a": True, "feature_b": True},
            quiet=True,
        )
    assert "keep.txt.gate" in str(exc_info.value)


def test_gate_composes_with_exclude(template: Path, tmp_path: Path) -> None:
    """A path excluded by `_exclude` is still skipped even if its gate is truthy."""
    run_copy(
        str(template),
        tmp_path,
        data={"feature_a": True, "feature_b": True},
        quiet=True,
        exclude=["always.txt"],
    )
    assert not (tmp_path / "always.txt").exists()
    assert (tmp_path / "keep.txt").exists()
