from __future__ import annotations

import pytest

import copier
from copier import evaluate_questionnaire
from copier._types import QuestionnaireResult
from copier.errors import UserMessageError

from .helpers import build_file_tree


def test_evaluate_questionnaire_full_data(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """All answers supplied via `data` are resolved verbatim."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                name:
                    type: str
                version:
                    type: str
                """
            ),
        }
    )
    result = evaluate_questionnaire(
        str(src),
        data={"name": "x", "version": "1.0.0"},
        defaults=True,
    )
    assert isinstance(result, QuestionnaireResult)
    assert result.answers["name"] == "x"
    assert result.answers["version"] == "1.0.0"
    assert result.skipped == []


def test_evaluate_questionnaire_partial_data_uses_defaults(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Questions not in `data` fall back to their default."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                name:
                    type: str
                    default: world
                """
            ),
        }
    )
    result = evaluate_questionnaire(str(src), data={}, defaults=True)
    assert result.answers["name"] == "world"
    assert result.skipped == []


def test_evaluate_questionnaire_data_overrides_default(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """`data` takes precedence over the template default."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                name:
                    type: str
                    default: world
                """
            ),
        }
    )
    result = evaluate_questionnaire(str(src), data={"name": "custom"}, defaults=True)
    assert result.answers["name"] == "custom"


def test_evaluate_questionnaire_when_skip_listed_in_skipped(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Questions whose `when:` is falsy are listed in `skipped` and omitted."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                name:
                    type: str
                    default: world
                enabled:
                    type: str
                    default: hello
                    when: false
                enabled_with_default:
                    type: str
                    default: hi
                    when: false
                """
            ),
        }
    )
    result = evaluate_questionnaire(str(src), defaults=True)
    assert "enabled" in result.skipped
    assert "enabled_with_default" in result.skipped
    assert result.answers["name"] == "world"
    # Skipped questions keep their default in the combined answers (they are
    # only omitted from what would be written to the answers file), mirroring
    # `run_copy` semantics.
    assert result.answers["enabled"] == "hello"
    assert result.answers["enabled_with_default"] == "hi"
    # They are excluded from the answers file data.
    assert "enabled" not in result.answers_file_data
    assert "enabled_with_default" not in result.answers_file_data


def test_evaluate_questionnaire_when_template_renders_skipped(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A `when:` templated against an earlier answer evaluates correctly."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                mode:
                    type: str
                    default: simple
                advanced:
                    type: str
                    default: deep
                    when: "{{ mode == 'advanced' }}"
                """
            ),
        }
    )
    skipped_mode = evaluate_questionnaire(str(src), defaults=True)
    assert "advanced" in skipped_mode.skipped

    result = evaluate_questionnaire(str(src), data={"mode": "advanced"}, defaults=True)
    assert "advanced" not in result.skipped
    assert result.answers["advanced"] == "deep"


def test_evaluate_questionnaire_strict_undefined_does_not_blow_up(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A StrictUndefined template is not rendered, so it doesn't blow up."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                _envops:
                    undefined: jinja2.StrictUndefined
                name:
                    type: str
                    default: world
                """
            ),
        }
    )
    result = evaluate_questionnaire(str(src), defaults=True)
    assert result.answers["name"] == "world"


def test_evaluate_questionnaire_context_produced(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """`context` mirrors what `_render_context` produces for the answers."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                name:
                    type: str
                    default: world
                """
            ),
        }
    )
    result = evaluate_questionnaire(str(src), defaults=True)
    assert "_copier_conf" in result.context
    assert result.context["name"] == "world"
    assert result.context["_copier_python"]


def test_evaluate_questionnaire_missing_required_raises(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A required answer with no default raises UserMessageError listing names."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                name:
                    type: str
                """
            ),
        }
    )
    with pytest.raises(UserMessageError) as exc_info:
        evaluate_questionnaire(str(src), defaults=True)
    assert "name" in exc_info.value.message
    assert "required" in exc_info.value.message.lower()


def test_evaluate_questionnaire_missing_required_lists_all_names(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """All missing required questions are listed, not just the first."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                name:
                    type: str
                version:
                    type: str
                """
            ),
        }
    )
    with pytest.raises(UserMessageError) as exc_info:
        evaluate_questionnaire(str(src), defaults=True)
    message = exc_info.value.message
    assert "name" in message
    assert "version" in message


def test_evaluate_questionnaire_is_public() -> None:
    """The function is part of the public, non-deprecated API surface."""
    assert evaluate_questionnaire is copier.evaluate_questionnaire
    assert "evaluate_questionnaire" in copier.__all__


def test_evaluate_questionnaire_does_not_render_files(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """No files are written to the template or a destination directory."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                name:
                    type: str
                    default: world
                """
            ),
            src / "{{ name }}.txt.jinja": "Hello, {{ name }}!",
        }
    )
    result = evaluate_questionnaire(str(src), defaults=True)
    assert result.answers["name"] == "world"
    # The rendered template file should NOT have been materialized anywhere in src.
    assert not (src / "world.txt").exists()


def test_evaluate_questionnaire_returns_rendered_questions(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """`questions` exposes each question's rendered spec (name/type/help/
    choices/default/when) so external drivers know what is being asked."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                color:
                    type: str
                    help: Pick a color
                    choices:
                        - red
                        - blue
                    default: red
                advanced:
                    type: bool
                    when: "{{ color == 'red' }}"
                    default: false
                note:
                    type: str
                    ask: false
                    default: internal
                """
            ),
        }
    )
    result = evaluate_questionnaire(str(src), defaults=True)

    specs = {q.var_name: q for q in result.questions}
    assert list(specs) == ["color", "advanced", "note"]

    color = specs["color"]
    assert color.type == "str"
    assert color.help == "Pick a color"
    assert color.choices == ["red", "blue"]
    assert color.default == "red"
    assert color.required is False
    assert color.when is True

    advanced = specs["advanced"]
    assert advanced.type == "bool"
    assert advanced.when is True

    note = specs["note"]
    assert note.ask is False


def test_evaluate_questionnaire_questions_skip_when_false(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Questions whose `when:` evaluates false report `when=False` and are
    skipped, while still being listed in `questions`."""
    src = tmp_path_factory.mktemp("src")
    build_file_tree(
        {
            src / "copier.yml": (
                """\
                first:
                    type: str
                    default: a
                second:
                    type: str
                    when: "{{ first == 'b' }}"
                    default: x
                """
            ),
        }
    )
    result = evaluate_questionnaire(str(src), defaults=True)

    spec = {q.var_name: q for q in result.questions}["second"]
    assert spec.when is False
    assert "second" in result.skipped
    assert "second" not in result.answers_file_data
