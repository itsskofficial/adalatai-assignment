""".env.example lists every setting the tool reads, so it cannot go stale.

The settings are found by reading the source: every name of the form INVOICE_COLLECTOR_...
and every model key (..._API_KEY) written in a string in backend/src. A setting added to the
code and not to .env.example fails here.
"""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "backend" / "src"
EXAMPLE = REPO / ".env.example"
NAME = re.compile(r"\b(INVOICE_COLLECTOR_[A-Z0-9_]*[A-Z0-9]|[A-Z][A-Z0-9]*_API_KEY)\b")
LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def settings_read_by_the_code() -> set[str]:
    found: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.update(NAME.findall(node.value))
    return found


def listed() -> dict[str, tuple[str, list[str]]]:
    """Each setting in .env.example, with its value and the comment lines just above it."""
    settings: dict[str, tuple[str, list[str]]] = {}
    comment: list[str] = []
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            comment.append(line)
            continue
        match = LINE.match(line)
        if match:
            settings[match.group(1)] = (match.group(2), comment)
        else:
            comment = []
    return settings


def test_the_source_is_searched_for_settings() -> None:
    found = settings_read_by_the_code()

    assert {"INVOICE_COLLECTOR_SESSION_SECRET", "ANTHROPIC_API_KEY", "JEV_API_KEY"} <= found


def test_every_setting_the_code_reads_is_in_the_example() -> None:
    missing = settings_read_by_the_code() - set(listed())

    assert not missing, f"add these to .env.example, each with what it is for: {sorted(missing)}"


def test_the_example_holds_no_value() -> None:
    given = {name: value for name, (value, _) in listed().items() if value.strip()}

    assert given == {}


def test_each_setting_says_what_it_is_for_whether_it_is_required_and_its_default() -> None:
    for name, (_, comment) in listed().items():
        said = " ".join(comment)
        assert comment, f"{name} has no line saying what it is for"
        assert re.search(r"\b(Required|Optional)\b", said), f"{name}: required or not?"
        assert re.search(r"\b(Defaults?|No default)\b", said), f"{name}: what is its default?"
