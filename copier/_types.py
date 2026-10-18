"""Complex types, annotations, validators."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Literal,
    NewType,
    TypeVar,
)

from pydantic import AfterValidator

# simple types
StrOrPath = str | Path
AnyByStrDict = dict[str, Any]
AnyByStrMutableMapping = MutableMapping[str, Any]

# sequences
IntSeq = Sequence[int]
PathSeq = Sequence[Path]

# optional types
OptBool = bool | None
OptStrOrPath = StrOrPath | None

# miscellaneous
T = TypeVar("T")
JSONSerializable = (dict, list, str, int, float, bool, type(None))
VCSTypes = Literal["git"]
Env = Mapping[str, str]
MissingType = NewType("MissingType", object)
MISSING = MissingType(object())
Operation = Literal["copy", "update"]


# Validators
def path_is_absolute(value: Path) -> Path:
    """Require absolute paths in an argument."""
    if not value.is_absolute():
        from .errors import PathNotAbsoluteError

        raise PathNotAbsoluteError(path=value)
    return value


def path_is_relative(value: Path) -> Path:
    """Require relative paths in an argument."""
    if value.is_absolute():
        from .errors import PathNotRelativeError

        raise PathNotRelativeError(path=value)
    return value


AbsolutePath = Annotated[Path, AfterValidator(path_is_absolute)]
RelativePath = Annotated[Path, AfterValidator(path_is_relative)]


_K = TypeVar("_K")
_V = TypeVar("_V")


# HACK https://github.com/copier-org/copier/pull/1880#discussion_r1887491497
class LazyDict(MutableMapping[_K, _V]):
    """A dict where values are functions that get evaluated only once when requested."""

    def __init__(self, mapping: Mapping[_K, Callable[[], _V]] | None = None):
        self._pending = dict(mapping or {})
        self._done: dict[_K, _V] = {}

    def __getitem__(self, key: _K) -> _V:
        if key not in self._done:
            self._done[key] = self._pending[key]()
        return self._done[key]

    def __setitem__(self, key: _K, value: _V) -> None:
        self._pending[key] = lambda: value
        self._done.pop(key, None)

    def __delitem__(self, key: _K) -> None:
        del self._pending[key]
        del self._done[key]

    def __iter__(self) -> Iterator[_K]:
        return iter(self._pending)

    def __len__(self) -> int:
        return len(self._pending)


class Phase(str, Enum):
    """The known execution phases."""

    PROMPT = "prompt"
    TASKS = "tasks"
    MIGRATE = "migrate"
    RENDER = "render"
    UNDEFINED = "undefined"

    def __str__(self) -> str:
        return str(self.value)

    @classmethod
    @contextmanager
    def use(cls, phase: Phase) -> Iterator[None]:
        """Set the current phase for the duration of a context."""
        token = _phase.set(phase)
        try:
            yield
        finally:
            _phase.reset(token)

    @classmethod
    def current(cls) -> Phase:
        """Get the current phase."""
        return _phase.get()


_phase: ContextVar[Phase] = ContextVar("phase", default=Phase.UNDEFINED)


class VcsRef(Enum):
    CURRENT = ":current:"
    """A special value to indicate that the current ref of the existing
    template should be used.
    """


@dataclass
class QuestionSpec:
    """A question of a template's questionnaire, rendered for the current answers.

    Produced by `evaluate_questionnaire`. Field names mirror the question
    keys accepted in `copier.yml`; templated fields hold the *rendered* value
    evaluated against the answers already collected, exactly as the
    interactive ask loop sees them.

    Attributes:
        var_name: The answer key (`var_name` in `copier.yml`).
        type: Rendered question type (`str`, `bool`, `int`, `yaml`, `path`, ...).
        help: Rendered help text (`""` when unset).
        default: The rendered default, or `None` when the question has none.
        required: `True` when no default applies — an answer must be provided.
        choices: Rendered choices for choice-type questions (`[]` otherwise).
        multiselect: Whether multiple choices are allowed.
        secret: Whether the answer is treated as secret.
        ask: `False` when the template declares `ask: false` (never prompted).
        when: The evaluated visibility (`when:`) given the collected answers.
        placeholder: Rendered placeholder text.
        validator: The raw `validator` template (rendering needs a candidate
            answer; use it via `parse/validate` semantics on the caller side
            or inspect the template string).
    """

    var_name: str
    type: str
    help: str
    default: Any
    required: bool
    choices: list[Any]
    multiselect: bool
    secret: bool
    ask: bool
    when: bool
    placeholder: str
    validator: str


@dataclass
class QuestionnaireResult:
    """Result of evaluating a template's questionnaire without rendering.

    Attributes:
        questions:
            The questionnaire's questions as `QuestionSpec` objects, in
            declaration order, rendered against the collected answers. Lets
            embedders (form builders, agents driving copier externally)
            discover what is being asked — names, types, help, choices,
            defaults, `when`/`ask` outcomes — without importing internals.

        answers:
            The resolved answers to the questionnaire, after applying `data`,
            defaults and `when` handling. Same semantics as the answers
            `Worker._ask` produces during a `copy` (i.e. the combined answers,
            including defaults computed for skipped questions).

        skipped:
            Variable names of questions whose `when:` condition evaluated to a
            falsy value. Such questions are still present in `answers` (with
            their default, if any) but are omitted from
            `answers_file_data`, exactly as `Worker._ask` omits them from the
            answers file.

        context:
            The rendering context that `_render_context` would produce for this
            answer set. Useful for downstream rendering by the caller.
    """

    questions: list[QuestionSpec]
    answers: AnyByStrDict
    skipped: list[str]
    context: AnyByStrDict

    @property
    def _skipped_set(self) -> set[str]:
        return set(self.skipped)

    @property
    def answers_file_data(self) -> AnyByStrDict:
        """Answers that would be written to the copier answers file.

        Mirrors `Worker._answers_to_remember`: excludes internal (`_`-prefixed)
        keys and questions skipped via a falsy `when`.
        """
        return {
            key: value
            for key, value in self.answers.items()
            if not key.startswith("_") and key not in self._skipped_set
        }
