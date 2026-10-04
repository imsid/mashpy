"""The single source of expression meanings and their bundled animation clips."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ExpressionDefinition:
    meaning: str
    asset: str


EXPRESSIONS: Mapping[str, ExpressionDefinition] = MappingProxyType({
    "curious": ExpressionDefinition(
        meaning="exploring possibilities, open and interested",
        asset="curious.gif",
    ),
    "focused": ExpressionDefinition(
        meaning="concentrating on a clear approach",
        asset="focused.gif",
    ),
    "puzzled": ExpressionDefinition(
        meaning="uncertain, reconsidering, or stepping back",
        asset="puzzled.gif",
    ),
    "pleased": ExpressionDefinition(
        meaning="a satisfying realization or expressed satisfaction",
        asset="pleased.gif",
    ),
})
