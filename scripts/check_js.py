"""Syntax-check the JavaScript that gets injected into the chart page.

This exists because the same class of bug has now broken the page twice, and
both times it broke *everything* rather than one feature -- a syntax error kills
the whole <script> block, so playback, the view tabs and the nav links all die
together and none of them look related to the change that caused it.

Both bugs came from the same place: the JS lives inside ordinary Python
strings, so Python gets to interpret it first.

  * `_TEMPLATE` is passed through `.format()`, so a single `{` or `}` in the
    JavaScript is read as a format field.
  * `_NAV_JS` is *not* a format string but *is* a regular (non-raw) string, so
    `\\n` inside a JS string literal became a real newline and split the literal
    across two lines.

Neither is caught by importing the module, rendering a page, or by any Python
linter -- the output is a valid Python string either way. Only a JS parser
notices, which is what this does.

    uv run python scripts/check_js.py

Exits non-zero on a syntax error, so it can go in a pre-commit hook.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def check(name: str, source: str) -> bool:
    """Parse a fragment with node. Returns True when it's valid."""
    # Wrapped in a function so references to variables defined elsewhere on the
    # page (audio, update, …) don't matter -- node --check parses without
    # resolving names.
    wrapped = f"(function () {{\n{source}\n}});"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(wrapped)
        path = handle.name

    result = subprocess.run(
        ["node", "--check", path], capture_output=True, text=True
    )
    Path(path).unlink(missing_ok=True)

    if result.returncode == 0:
        print(f"  ok    {name}  ({len(source):,} chars)")
        return True

    print(f"  FAIL  {name}")
    for line in result.stderr.strip().splitlines()[:12]:
        print(f"        {line}")
    return False


def main() -> None:
    from chord_detect import render

    print("Checking injected JavaScript:")
    ok = True

    # The nav/lyrics block, injected as a .format() argument.
    ok &= check("_NAV_JS", render._NAV_JS)

    # The main player script, which lives inside the format string itself. Undo
    # the brace doubling to recover the JavaScript as the browser sees it.
    template = render._TEMPLATE
    start = template.index("<script>\n(function () {{")
    end = template.index("</script>", start)
    player = template[start + len("<script>") : end]
    player = player.replace("{{", "{").replace("}}", "}")
    # Placeholders like {timeline} become bare identifiers, which parse fine.
    for field in ("timeline", "options", "key", "sounding", "capo0", "navjs"):
        player = player.replace("{" + field + "}", f"__{field}__")
    ok &= check("player script", player)

    if not ok:
        print("\nA syntax error here breaks the entire page, not one feature.")
        sys.exit(1)
    print("\nAll injected JavaScript parses.")


if __name__ == "__main__":
    main()
