"""Parser adapters for RepoGauge-to-harness bridge."""

from .junit import (
    get_parser,
    parse_repogauge_junit,
    parse_repogauge_test_output,
    register_parser,
)

__all__ = [
    "get_parser",
    "parse_repogauge_junit",
    "parse_repogauge_test_output",
    "register_parser",
]
