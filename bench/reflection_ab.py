"""Paired test of strategic reflection on stall-prone tasks against a live llama-server.

Each task runs in a fresh copy of its fixture under each configuration ("arm"):

  linear   no forks, like `refl --linear`
  reflect  strategic reflection only, like `refl --no-verify`
  full     verification and reflection, like plain `refl`

`linear` and `reflect` send the same system prompt, tools, task prompt, and request budget, so the
arms differ only in whether stall detection can start reflection batches. llama-server is not
guaranteed to repeat a trajectory exactly, even at temperature 0, because prompt-cache reuse can
change the numerics. Use --repeat for more than a smoke test.

By default each run starts from the task prompt, and reflection happens only if the model stalls.
A capable model may never stall on these tasks. With --seeded, each run first replays a scripted
stall on the main branch with real tool results, such as three identical test failures after edits.
The reflect arm then starts a reflection batch, and the linear arm continues without one. Both arms
then get the same request budget, which also pays for the reviewers in the reflect arm.

--heat sets the reflection temperature for the seeded stall, which selects lateral roles such as
Analogy. After each reflection the model groups proposals that recommend the same next action, and
the summary shows the number of distinct ideas.

The script replays Reflect's stall detector over each main branch to show where the linear arm would
have reflected. Workspaces and results go to a directory outside this repository, so Bash commands
that the model runs cannot reach this checkout's Git repository.

    python bench/reflection_ab.py --validate
    python bench/reflection_ab.py --url http://127.0.0.1:8080
    python bench/reflection_ab.py --seeded --repeat 2
    python bench/reflection_ab.py --seeded --heat 2 --arms reflect
    python bench/reflection_ab.py --judge RESULTS_DIR
"""

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from refl.client import LlamaClient, ServerError  # noqa: E402
from refl.engine import (  # noqa: E402
    _EVIDENCE_PROMPT, _PROPOSAL_PROMPT, _VERIFY_PROMPT, SYSTEM_PROMPT, THINKING_MODES, Engine, TurnLimitError,
)
from refl.reflection import REFLECTION_PROMPT, ROLES, StallDetector  # noqa: E402
from refl.tools import WorkspaceTools  # noqa: E402
from refl.tree import ConversationTree  # noqa: E402

ARMS = {
    "linear": {"forks": False, "verification": False},
    "reflect": {"forks": True, "verification": False},
    "full": {"forks": True, "verification": True},
}


# Fixture helpers ---------------------------------------------------------------------------------

def _python(workspace, *args):
    return subprocess.run(
        [sys.executable, *args], cwd=workspace, stdin=subprocess.DEVNULL, capture_output=True,
        encoding="utf-8", errors="replace", timeout=60,
    )


def _passes(workspace, script):
    result = _python(workspace, script)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0 and output.endswith("OK"), output[-300:]


def _unchanged(workspace, task, path):
    expected = task.files[path]
    expected = expected.encode("utf-8") if isinstance(expected, str) else expected
    actual = (workspace / path).read_bytes() if (workspace / path).is_file() else None
    return actual == expected


@dataclass
class Task:
    name: str
    trap: str
    prompt: str
    files: dict
    check: Callable
    reference: Callable
    # Main-branch tool calls for --seeded runs, as (tool name, arguments). The last call must trigger
    # the stall detector and no earlier call may.
    stall: list = field(default_factory=list)


# Task 1: the test imports a stale vendored copy, so edits to app.py do not change the failure.

_SLUGIFY = '''def slugify(text):
    """Return a lowercase, hyphen-separated slug."""
    return text.lower().replace(" ", "-")
'''
_SHADOWED = Task(
    name="shadowed-module",
    trap="Edits to app.py leave the failure unchanged because a helper puts vendor/ first on sys.path.",
    prompt=("`python test_app.py` fails. Fix slugify in app.py so that the test prints OK. "
            "Do not modify test_app.py."),
    files={
        "app.py": _SLUGIFY,
        "vendor/app.py": _SLUGIFY,
        "testsupport.py": (
            '"""Shared test setup."""\n'
            "import os\nimport sys\n\n"
            'sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))\n'
        ),
        "test_app.py": (
            "import testsupport  # noqa: F401\n"
            "from app import slugify\n\n"
            'result = slugify("Hello, World!")\n'
            'if result != "hello-world":\n'
            "    raise SystemExit(f\"FAILED: slugify('Hello, World!') returned {result!r}, expected 'hello-world'\")\n"
            'print("OK")\n'
        ),
    },
    check=lambda ws: _check_shadowed(ws),
    reference=lambda ws: _fix_shadowed(ws),
    # Two correct fixes to app.py that cannot change the failure, because the test imports vendor/app.py.
    stall=[
        ("read_file", {"path": "app.py"}),
        ("bash", {"command": "python test_app.py"}),
        ("write_file", {"path": "app.py", "content": (
            'import re\n\n\ndef slugify(text):\n    """Return a lowercase, hyphen-separated slug."""\n'
            '    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")\n')}),
        ("bash", {"command": "python test_app.py"}),
        ("write_file", {"path": "app.py", "content": (
            'import re\n\n\ndef slugify(text):\n    """Return a lowercase, hyphen-separated slug."""\n'
            '    words = re.findall(r"[a-z0-9]+", text.lower())\n    return "-".join(words)\n')}),
        ("bash", {"command": "python test_app.py"}),
    ],
)


def _check_shadowed(ws):
    if not _unchanged(ws, _SHADOWED, "test_app.py"):
        return False, "test_app.py was modified"
    passed, output = _passes(ws, "test_app.py")
    if not passed:
        return False, output
    probe = _python(ws, "-c", (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('root_app', 'app.py')\n"
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
        "sys.exit(0 if module.slugify('Hello, World!') == 'hello-world' else 1)\n"
    ))
    return (True, "test passes and app.py is fixed") if probe.returncode == 0 else (
        False, "test passes but app.py itself is still wrong")


def _fix_shadowed(ws):
    (ws / "app.py").write_text(
        "import re\n\n\ndef slugify(text):\n"
        '    return "-".join(re.findall(r"[a-z0-9]+", text.lower()))\n', encoding="utf-8")
    (ws / "vendor" / "app.py").unlink()


# Task 2: the build cache hashes the wrong input, so `python build.py` keeps skipping the render.

_SITE = '{"title": "Demo"}\n'
_STALE = Task(
    name="stale-build",
    trap="build.py reports success but skips rendering because its cache ignores template.html.",
    prompt=("Change the page heading from Hello to Hello, Reflect by editing template.html, then rebuild "
            "with `python build.py`. out/index.html must show the new heading."),
    files={
        "site.json": _SITE,
        "template.html": "<h1>Hello</h1>\n<p>Generated page.</p>\n",
        "out/index.html": "<!-- generated by build.py -->\n<h1>Hello</h1>\n<p>Generated page.</p>\n",
        ".cache/inputs.sha256": hashlib.sha256(_SITE.encode("utf-8")).hexdigest(),
        "build.py": (
            '"""Render template.html into out/index.html when the inputs changed."""\n'
            "import hashlib\nimport sys\nfrom pathlib import Path\n\n"
            "ROOT = Path(__file__).parent\n"
            'CACHE = ROOT / ".cache" / "inputs.sha256"\n'
            'OUT = ROOT / "out" / "index.html"\n\n\n'
            "def digest():\n"
            "    # Hash the build inputs so unchanged inputs skip the render step.\n"
            '    return hashlib.sha256((ROOT / "site.json").read_bytes()).hexdigest()\n\n\n'
            "def main():\n"
            '    force = "--force" in sys.argv\n'
            "    current = digest()\n"
            "    if not force and CACHE.exists() and CACHE.read_text() == current:\n"
            '        print("Inputs unchanged, skipping render.")\n'
            "        return\n"
            "    OUT.parent.mkdir(exist_ok=True)\n"
            '    OUT.write_text("<!-- generated by build.py -->\\n" + (ROOT / "template.html").read_text())\n'
            "    CACHE.parent.mkdir(exist_ok=True)\n"
            "    CACHE.write_text(current)\n"
            '    print(f"Rendered {OUT.relative_to(ROOT).as_posix()}")\n\n\n'
            "main()\n"
        ),
    },
    check=lambda ws: _check_stale(ws),
    reference=lambda ws: _fix_stale(ws),
    # The template edit succeeds, then the build reports success three times without rendering.
    stall=[
        ("read_file", {"path": "template.html"}),
        ("edit_file", {"path": "template.html", "old_text": "<h1>Hello</h1>", "new_text": "<h1>Hello, Reflect</h1>"}),
        ("bash", {"command": "python build.py"}),
        ("bash", {"command": "python build.py"}),
        ("bash", {"command": "python build.py"}),
    ],
)


def _check_stale(ws):
    template = (ws / "template.html").read_text(encoding="utf-8")
    output = (ws / "out" / "index.html").read_text(encoding="utf-8")
    if "<h1>Hello, Reflect</h1>" not in template:
        return False, "template.html does not have the new heading"
    if output != "<!-- generated by build.py -->\n" + template:
        return False, "out/index.html does not match the rendered template"
    return True, "out/index.html renders the new heading"


def _fix_stale(ws):
    path = ws / "template.html"
    path.write_text(path.read_text(encoding="utf-8").replace("<h1>Hello</h1>", "<h1>Hello, Reflect</h1>"),
                    encoding="utf-8")
    _python(ws, "build.py", "--force")


# Task 3: the config file is Latin-1, which read_file and edit_file cannot decode.

_LEGACY = "[machine]\nname = Fräse 3\nmaterial = Stahl, Größe M\ntimeout = 30\nretries = 2\n"
_LATIN1 = Task(
    name="latin1-config",
    trap="read_file and edit_file only handle UTF-8, so the obvious edit keeps failing on a Latin-1 file.",
    prompt=("Change the timeout in legacy.ini from 30 to 60. The file is used by an old tool that expects "
            "its current encoding, so keep the encoding and every other byte unchanged."),
    files={"legacy.ini": _LEGACY.encode("latin-1")},
    check=lambda ws: _check_latin1(ws),
    reference=lambda ws: (ws / "legacy.ini").write_bytes(
        _LEGACY.replace("timeout = 30", "timeout = 60").encode("latin-1")),
    # The UTF-8 file tools reject the file, and the same edit is retried.
    stall=[
        ("read_file", {"path": "legacy.ini"}),
        ("edit_file", {"path": "legacy.ini", "old_text": "timeout = 30", "new_text": "timeout = 60"}),
        ("edit_file", {"path": "legacy.ini", "old_text": "timeout = 30", "new_text": "timeout = 60"}),
    ],
)


def _check_latin1(ws):
    expected = _LEGACY.replace("timeout = 30", "timeout = 60").encode("latin-1")
    actual = (ws / "legacy.ini").read_bytes()
    if actual == expected:
        return True, "legacy.ini has the new timeout in its original encoding"
    if actual == _LEGACY.encode("latin-1"):
        return False, "legacy.ini is unchanged"
    return False, f"legacy.ini differs from the expected bytes: {actual[:120]!r}"


# Task 4: a control without a trap. The wrong VAT rate is visible in the test output and rates.json.

_RATES = Task(
    name="invoice-rates",
    trap="None. A control task that a capable model should solve without stalling.",
    prompt=("The invoice totals are wrong for some countries. Fix it so that `python test_invoice.py` "
            "prints OK. Do not modify test_invoice.py."),
    files={
        "rates.json": '{"DE": 0.19, "AT": 0.2, "FR": 0.02}\n',
        "invoice.py": (
            "import json\nfrom pathlib import Path\n\n"
            'RATES = json.loads((Path(__file__).parent / "rates.json").read_text())\n\n\n'
            "def total(net, country):\n"
            '    """Gross amount rounded to cents."""\n'
            "    return round(net * (1 + RATES[country]), 2)\n"
        ),
        "test_invoice.py": (
            "from invoice import total\n\n"
            'cases = [(100, "DE", 119.0), (100, "AT", 120.0), (50, "FR", 60.0)]\n'
            "failed = [(net, country, expected, total(net, country)) for net, country, expected in cases\n"
            "          if total(net, country) != expected]\n"
            "if failed:\n"
            '    raise SystemExit(f"FAILED (net, country, expected, actual): {failed}")\n'
            'print("OK")\n'
        ),
    },
    check=lambda ws: _check_rates(ws),
    reference=lambda ws: (ws / "rates.json").write_text('{"DE": 0.19, "AT": 0.2, "FR": 0.2}\n', encoding="utf-8"),
)


def _check_rates(ws):
    if not _unchanged(ws, _RATES, "test_invoice.py"):
        return False, "test_invoice.py was modified"
    if '"FR"' in (ws / "invoice.py").read_text(encoding="utf-8"):
        return False, "invoice.py special-cases FR"
    return _passes(ws, "test_invoice.py")


# Task 5: an approach stall. The cause is known, but tuning the exponential recursion cannot fix it.

_PATHS = '''def count(rows, cols):
    """Count the monotonic lattice paths from (0, 0) to (rows, cols)."""
    if rows == 0 or cols == 0:
        return 1
    return count(rows - 1, cols) + count(rows, cols - 1)
'''
_PATH_CASES = [((3, 3), 20), ((1, 7), 8), ((30, 30), math.comb(60, 30)), ((25, 31), math.comb(56, 25))]
_CHECK_PATHS = (
    "import subprocess\nimport sys\n\n"
    f"CASES = {_PATH_CASES!r}\n\n"
    "for (rows, cols), expected in CASES:\n"
    "    code = f\"from paths import count; print(count({rows}, {cols}))\"\n"
    "    try:\n"
    "        result = subprocess.run([sys.executable, \"-c\", code], capture_output=True, text=True, timeout=5)\n"
    "    except subprocess.TimeoutExpired:\n"
    "        raise SystemExit(f\"FAILED: count({rows}, {cols}) did not finish within 5 s\")\n"
    "    if result.stdout.strip() != str(expected):\n"
    "        actual = result.stdout.strip() or result.stderr.strip()[-200:]\n"
    "        raise SystemExit(f\"FAILED: count({rows}, {cols}) returned {actual!r}\")\n"
    "print(\"OK\")\n"
)
_SLOW = Task(
    name="slow-paths",
    trap="An approach stall: the recursion is exponential, so tuning it keeps timing out.",
    prompt=("`python check.py` fails because count in paths.py is too slow. Make it fast enough that the check "
            "prints OK. Keep the signature of count and do not modify check.py."),
    files={"paths.py": _PATHS, "check.py": _CHECK_PATHS},
    check=lambda ws: _check_slow(ws),
    reference=lambda ws: (ws / "paths.py").write_text(
        "from functools import lru_cache\n\n\n@lru_cache(maxsize=None)\n" + _PATHS, encoding="utf-8"),
    # Two micro-optimizations that keep the exponential running time, each followed by the same timeout.
    stall=[
        ("read_file", {"path": "paths.py"}),
        ("bash", {"command": "python check.py"}),
        ("write_file", {"path": "paths.py", "content": (
            'def count(rows, cols):\n    """Count the monotonic lattice paths from (0, 0) to (rows, cols)."""\n'
            "    total = 0\n    stack = [(rows, cols)]\n    while stack:\n        r, c = stack.pop()\n"
            "        if r == 0 or c == 0:\n            total += 1\n        else:\n"
            "            stack.append((r - 1, c))\n            stack.append((r, c - 1))\n    return total\n")}),
        ("bash", {"command": "python check.py"}),
        ("write_file", {"path": "paths.py", "content": (
            'def count(rows, cols):\n    """Count the monotonic lattice paths from (0, 0) to (rows, cols)."""\n'
            "    if rows > cols:\n        rows, cols = cols, rows\n    if rows == 0:\n        return 1\n"
            "    return count(rows - 1, cols) + count(rows, cols - 1)\n")}),
        ("bash", {"command": "python check.py"}),
    ],
)


def _check_slow(ws):
    if not _unchanged(ws, _SLOW, "check.py"):
        return False, "check.py was modified"
    if any(str(expected) in (ws / "paths.py").read_text(encoding="utf-8") for _, expected in _PATH_CASES[2:]):
        return False, "paths.py hard-codes an expected result"
    return _passes(ws, "check.py")


# Task 6: deviations from a spec behind a check that only compares a hash. Fixing some of them leaves
# the failure message unchanged, so partial progress looks like no progress. Several spec rules defeat
# the obvious standard-library calls: .title() breaks "King's Lynn", string.capwords breaks
# "North-East", and strip() keeps a zero-width space.

_ZWSP = "​"
_SALES = f"""date,region,product,quantity,unit_price
2026-10-01,North,"Widget, large",3,12.995
2026-10-02, north,Gadget,10,4.125
2026-10-03,south,Nut,40,0.875
2026-10-04,{_ZWSP}East,"Bolt, M4",120,0.335
2026-10-05,NORTH ,Gadget,-2,4.125
2026-10-06,king's lynn,"Widget, large",1,2.675
2026-10-07,east ,Nut,25,0.875
2026-10-08,King's Lynn,Gadget,4,4.125
2026-10-09,south,"Bolt, M4",200,0.335
2026-10-10,East,"Widget, large",2,12.995
2026-10-11,KING'S LYNN,Nut,-1,0.875
2026-10-12,North,"Bolt, M4",15,0.335
2026-10-13,NORTH-EAST,Gadget,6,4.125
2026-10-14,north-east ,"Bolt, M4",30,0.335
2026-10-15,Westmoreland Hills,"Widget, large",4,12.995
2026-10-16,westmoreland hills,Nut,-2,0.875
"""
# The grader also runs report.py on this dataset, so a hard-coded report does not pass.
_HIDDEN_SALES = f"""date,region,product,quantity,unit_price
2026-11-01,Central,"Cable, 2 m",7,3.145
2026-11-02, central,Plug,30,1.005
2026-11-03,bishop's stortford,"Cable, 2 m",-3,3.145
2026-11-04,Bishop's Stortford ,Switch,12,8.335
2026-11-05,{_ZWSP}west-end,Plug,9,1.005
2026-11-06,WEST-END,"Fuse, 5 A",100,0.125
2026-11-07,Harbor,Plug,4,1.005
2026-11-08,central,Switch,-1,8.335
"""


def _report_line(name, orders, units, revenue):
    return f"{name:<12}{orders:>8}{units:>8}{revenue:>13}"


def _region_name(raw):
    trimmed = re.sub(r"^[\s​]+|[\s​]+$", "", raw).casefold()
    return re.sub(r"[^\s-]+", lambda word: word.group(0)[:1].upper() + word.group(0)[1:], trimmed)


def _render_report(text):
    """The report that SPEC.md describes, for checking the task's results."""
    regions = {}
    for row in csv.DictReader(io.StringIO(text)):
        entry = regions.setdefault(_region_name(row["region"]), {"orders": 0, "units": 0, "revenue": Decimal(0)})
        quantity = int(row["quantity"])
        entry["orders"] += quantity > 0
        entry["units"] += quantity
        entry["revenue"] += quantity * Decimal(row["unit_price"])

    def money(value):
        return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"

    lines = ["Sales report", _report_line("Region", "Orders", "Units", "Revenue")]
    for name in sorted(regions, key=lambda name: (-regions[name]["revenue"], name)):
        entry = regions[name]
        label = name if len(name) <= 12 else name[:11] + "~"
        lines.append(_report_line(label, entry["orders"], entry["units"], money(entry["revenue"])))
    totals = {key: sum(entry[key] for entry in regions.values()) for key in ("orders", "units", "revenue")}
    lines.append(_report_line("Total", totals["orders"], totals["units"], money(totals["revenue"])))
    return "\n".join(lines) + "\n"


_SPEC = f"""# Sales report

`python report.py` reads `sales.csv` and writes `report.txt`.

## Input

`sales.csv` has the header `date,region,product,quantity,unit_price`. Product names may be quoted and contain
commas. A negative quantity is a refund. Unit prices have up to three decimal places.

## Output

`report.txt` is UTF-8 with LF line endings and a final newline. It looks like this, with different regions
and numbers:

```text
Sales report
{_report_line("Region", "Orders", "Units", "Revenue")}
{_report_line("North", 3, 12, "1,234.50")}
{_report_line("King's Lynn", 2, 9, "310.25")}
{_report_line("Stratford-U~", 1, 5, "98.00")}
{_report_line("Total", 6, 26, "1,642.75")}
```

- The first line is `Sales report`, followed by the column header.
- Each region gets one row. Region names are case-insensitive. Before grouping, remove spaces, non-breaking
  spaces, and zero-width spaces (U+200B) from the start and end of the name.
- Show each region name with every word capitalized: the first letter in upper case and the rest in lower
  case. Words are separated by spaces or hyphens. An apostrophe does not start a new word, so `king's lynn`
  is shown as `King's Lynn` and `NORTH-EAST` as `North-East`.
- A name longer than 12 characters is shown as its first 11 characters followed by `~`, so
  `stratford-upon-avon` is shown as `Stratford-U~`. Sorting uses the full name.
- Orders counts the rows with a positive quantity. Refunds are not orders.
- Units is the sum of all quantities, refunds included.
- Revenue is the sum of quantity times unit price, refunds included. Compute it exactly in decimal arithmetic
  and round half up to two decimals only when formatting, with a comma as the thousands separator.
- Regions are sorted by revenue, highest first. Regions with equal revenue are sorted by name.
- The last row, `Total`, sums the orders, units, and revenue of all regions.
- The region name is left-aligned in 12 characters. Orders and Units are right-aligned in 8 characters each,
  and Revenue in 13 characters.
"""
_REPORT_PY = '''"""Write report.txt from sales.csv as described in SPEC.md."""
import csv
from pathlib import Path

ROOT = Path(__file__).parent


def load(path):
    with open(path, newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def summarize(rows):
    regions = {}
    for row in rows:
        name = row["region"]
        quantity = int(row["quantity"])
        entry = regions.setdefault(name, {"orders": 0, "units": 0, "revenue": 0.0})
        entry["orders"] += 1
        entry["units"] += quantity
        entry["revenue"] += quantity * float(row["unit_price"])
    return regions


def money(value):
    return f"{round(value, 2):,.2f}"


def line(name, orders, units, revenue):
    return f"{name:<12}{orders:>8}{units:>8}{revenue:>13}"


def main():
    regions = summarize(load(ROOT / "sales.csv"))
    lines = ["Sales report", line("Region", "Orders", "Units", "Revenue")]
    for name in sorted(regions):
        entry = regions[name]
        lines.append(line(name, entry["orders"], entry["units"], money(entry["revenue"])))
    orders = sum(entry["orders"] for entry in regions.values())
    units = sum(entry["units"] for entry in regions.values())
    revenue = sum(entry["revenue"] for entry in regions.values())
    lines.append(line("Total", orders, units, money(revenue)))
    (ROOT / "report.txt").write_bytes(("\\n".join(lines) + "\\n").encode("utf-8"))


main()
'''
_REPORT_FIXED_PY = r'''"""Write report.txt from sales.csv as described in SPEC.md."""
import csv
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).parent


def load(path):
    with open(path, newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def region_name(raw):
    trimmed = re.sub(r"^[\s​]+|[\s​]+$", "", raw).casefold()
    return re.sub(r"[^\s-]+", lambda word: word.group(0)[:1].upper() + word.group(0)[1:], trimmed)


def summarize(rows):
    regions = {}
    for row in rows:
        name = region_name(row["region"])
        quantity = int(row["quantity"])
        entry = regions.setdefault(name, {"orders": 0, "units": 0, "revenue": Decimal(0)})
        if quantity > 0:
            entry["orders"] += 1
        entry["units"] += quantity
        entry["revenue"] += quantity * Decimal(row["unit_price"])
    return regions


def money(value):
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def label(name):
    return name if len(name) <= 12 else name[:11] + "~"


def line(name, orders, units, revenue):
    return f"{name:<12}{orders:>8}{units:>8}{revenue:>13}"


def main():
    regions = summarize(load(ROOT / "sales.csv"))
    lines = ["Sales report", line("Region", "Orders", "Units", "Revenue")]
    for name in sorted(regions, key=lambda name: (-regions[name]["revenue"], name)):
        entry = regions[name]
        lines.append(line(label(name), entry["orders"], entry["units"], money(entry["revenue"])))
    orders = sum(entry["orders"] for entry in regions.values())
    units = sum(entry["units"] for entry in regions.values())
    revenue = sum(entry["revenue"] for entry in regions.values())
    lines.append(line("Total", orders, units, money(revenue)))
    (ROOT / "report.txt").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


main()
'''
# Plausible first fixes for the seeded stall. The first one still misses the zero-width space and the
# apostrophe rule, so both leave the check failing with the same message.
_REPORT_FIXES = [
    ('        name = row["region"]\n', '        name = row["region"].strip().title()\n'),
    ('        entry["orders"] += 1\n', '        if quantity > 0:\n            entry["orders"] += 1\n'),
]
_CHECK_REPORT = (
    "import hashlib\nimport subprocess\nimport sys\nfrom pathlib import Path\n\n"
    f"EXPECTED = \"{hashlib.sha256(_render_report(_SALES).encode('utf-8')).hexdigest()}\"\n\n"
    "result = subprocess.run([sys.executable, \"report.py\"], capture_output=True, text=True)\n"
    "if result.returncode:\n"
    "    raise SystemExit(\"FAILED: report.py exited with an error:\\n\" + result.stderr[-500:])\n"
    "if hashlib.sha256(Path(\"report.txt\").read_bytes()).hexdigest() != EXPECTED:\n"
    "    raise SystemExit(\"FAILED: report.txt does not match the expected report.\")\n"
    "print(\"OK\")\n"
)


_REPORT = Task(
    name="opaque-report",
    trap=("report.py breaks several rules of SPEC.md, some of which defeat the obvious standard-library "
          "calls, and check.py only compares a hash, so fixing some of them leaves the failure unchanged."),
    prompt=("`python check.py` fails. Fix report.py so that report.txt follows SPEC.md exactly and the check "
            "prints OK. Do not modify check.py, sales.csv, or SPEC.md."),
    files={"SPEC.md": _SPEC, "sales.csv": _SALES, "report.py": _REPORT_PY, "check.py": _CHECK_REPORT},
    check=lambda ws: _check_report(ws),
    reference=lambda ws: (ws / "report.py").write_text(_REPORT_FIXED_PY, encoding="utf-8"),
    # Two correct fixes, each followed by the same opaque failure.
    stall=[
        ("read_file", {"path": "report.py"}),
        ("bash", {"command": "python check.py"}),
        ("edit_file", {"path": "report.py", "old_text": _REPORT_FIXES[0][0], "new_text": _REPORT_FIXES[0][1]}),
        ("bash", {"command": "python check.py"}),
        ("edit_file", {"path": "report.py", "old_text": _REPORT_FIXES[1][0], "new_text": _REPORT_FIXES[1][1]}),
        ("bash", {"command": "python check.py"}),
    ],
)


def _check_report(ws):
    for name in ("check.py", "sales.csv", "SPEC.md"):
        if not _unchanged(ws, _REPORT, name):
            return False, f"{name} was modified"
    passed, output = _passes(ws, "check.py")
    if not passed:
        # Partial credit: how far the report is from the spec, since the check itself only says no.
        expected = _render_report(_SALES).splitlines()
        report = ws / "report.txt"
        actual = report.read_text(encoding="utf-8", errors="replace").splitlines() if report.is_file() else []
        wrong = sum(1 for index in range(max(len(expected), len(actual)))
                    if index >= len(expected) or index >= len(actual) or expected[index] != actual[index])
        return False, f"{wrong} lines differ from the {len(expected)}-line expected report. {output}"
    with tempfile.TemporaryDirectory() as directory:
        hidden = Path(directory)
        for script in ws.glob("*.py"):
            if script.name != "check.py":
                shutil.copy(script, hidden / script.name)
        (hidden / "sales.csv").write_bytes(_HIDDEN_SALES.encode("utf-8"))
        _python(hidden, "report.py")
        actual = (hidden / "report.txt").read_bytes() if (hidden / "report.txt").is_file() else b""
    if actual != _render_report(_HIDDEN_SALES).encode("utf-8"):
        return False, "the check passes, but report.py gives a wrong report for the hidden dataset"
    return True, "report.txt follows SPEC.md for both datasets"


TASKS = {task.name: task for task in (_SHADOWED, _STALE, _LATIN1, _SLOW, _REPORT, _RATES)}


def materialize(task, workspace):
    for relative, content in task.files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


# Trajectory analysis ------------------------------------------------------------------------------

def _branch(system):
    for prompt, branch in ((_VERIFY_PROMPT, "verification"), (_EVIDENCE_PROMPT, "verification"),
                           (REFLECTION_PROMPT, "reflection"), (_PROPOSAL_PROMPT, "reflection"),
                           (_JUDGE_PROMPT, "judge")):
        if system.startswith(prompt):
            return branch
    return "main"


# Role names of the current pool and of the three fixed lenses that earlier versions used.
_ROLE_NAMES = {role.name for role in ROLES} | {"Assumptions", "Simpler approach"}
_WORD = re.compile(r"[a-z0-9_]+")
_COMMON = set("a an and are as at be by for from if in is it its of on or that the this to with "
              "basis cost fails abstract form".split())


def proposals(message):
    """Return (role, proposal) pairs from a "Reflection results" message."""
    found = []
    for line in message.splitlines():
        # Newer messages list proposals as "- Role: text" under evidence headings.
        name, separator, text = line.removeprefix("- ").partition(": ")
        if separator and name in _ROLE_NAMES:
            found.append((name, text))
    return found


def overlap(texts):
    """Mean pairwise Jaccard similarity of the proposals' words. Lower means more varied wording."""
    words = [set(_WORD.findall(text.lower())) - _COMMON for text in texts]
    pairs = [(a, b) for index, a in enumerate(words) for b in words[index + 1:] if a | b]
    return round(sum(len(a & b) / len(a | b) for a, b in pairs) / len(pairs), 2) if pairs else None


_JUDGE_PROMPT = (
    "You compare proposals that reviewers made for the same stalled coding task. Put proposals that recommend "
    "essentially the same next action into one group, even when they are worded differently. List every role "
    "name exactly once."
)
_JUDGE_SCHEMA = {
    "type": "json_object",
    "schema": {
        "type": "object",
        "properties": {"groups": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}},
        "required": ["groups"],
        "additionalProperties": False,
    },
}


async def distinct_ideas(server, model, ideas, max_tokens):
    """Ask the model how many distinct next actions the proposals contain."""
    if len(ideas) < 2:
        return len(ideas)
    messages = [{"role": "system", "content": _JUDGE_PROMPT},
                {"role": "user", "content": "\n".join(f"{name}: {text}" for name, text in ideas)}]
    try:
        message = await server.complete(model, messages, [], max_tokens, response_format=_JUDGE_SCHEMA)
        groups = json.loads(message.get("content") or "")["groups"]
        return len([group for group in groups if group])
    except (ServerError, KeyError, TypeError, ValueError):
        return None


def analyse(tree):
    """Summarize the main branch and replay the stall detector over its tool results."""
    path = []
    node_id = tree.head
    while node_id is not None:
        path.append(tree.nodes[node_id])
        node_id = tree.nodes[node_id].parent_id
    path.reverse()

    detector = StallDetector({"delay": 0, "remaining": 0})
    calls = {}
    results = failures = 0
    first_stall = first_reflection = None
    reflections = []
    for node in path:
        message = node.message
        if message["role"] == "assistant":
            for call in message.get("tool_calls") or []:
                calls[call["id"]] = call["function"]
        elif message["role"] == "tool":
            try:
                result = json.loads(message["content"])
            except (TypeError, ValueError):
                continue
            error = str(result.get("error", ""))
            if error.startswith(("Tool call deferred", "The conversation was resumed")):
                continue
            function = calls.get(message.get("tool_call_id"))
            if function is None:
                continue
            results += 1
            failures += "error" in result or result.get("exit_code", 0) != 0
            detector.record(function, result)
            if first_stall is None and (reason := detector.stall()):
                first_stall = {"after_tool_results": results, "reason": reason}
        elif message["role"] == "user" and str(message.get("content", "")).startswith("Reflection results"):
            ideas = proposals(message["content"])
            reflections.append({
                "roles": [name for name, _ in ideas],
                "overlap": overlap([text for _, text in ideas]),
                "message": message["content"][:4000],
            })
            if first_reflection is None:
                first_reflection = results
    return {
        "tool_results": results,
        "tool_failures": failures,
        "first_stall": first_stall,
        "reflections": len(reflections),
        "first_reflection_after_tool_results": first_reflection,
        "tool_results_after_first_reflection": None if first_reflection is None else results - first_reflection,
        "reflection_details": reflections,
    }


# Runner -------------------------------------------------------------------------------------------

async def seed_stall(engine, task):
    """Replay the task's scripted stall with real tool results. Return the reason and the step that fired."""
    engine.stalls.start_turn()
    engine.verdicts = {}
    engine.tree.append({"role": "user", "content": task.prompt})
    detector = StallDetector({"delay": 0, "remaining": 0})
    reason = fired_at = None
    for index, (name, arguments) in enumerate(task.stall):
        call = {"id": f"seed{index}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}
        leaf = engine.tree.append({"role": "assistant", "content": None, "tool_calls": [call]})
        await engine._observe(leaf, [call], stalls=engine.stalls if engine.forks else None)
        detector.record(call["function"], json.loads(engine.tree.nodes[engine.tree.head].message["content"]))
        if fired_at is None and (found := detector.stall()):
            reason, fired_at = found, index
    if engine.forks:
        # Let the engine's own detector report the stall so its pacing matches a natural reflection.
        reason = engine.stalls.stall()
    return reason, fired_at


async def respond_after_stall(engine, task, max_turns, heat=0):
    reason, _ = await seed_stall(engine, task)
    engine.tree.save()
    engine.requests_left = max_turns
    try:
        if engine.forks and reason:
            # Treat the seeded stall as the task's heat-th reflection, which raises the temperature.
            engine.stalls.batches = heat
            await engine._reflect(reason)
        return await engine._respond()
    except (ServerError, TurnLimitError) as error:
        engine.tree.append({"role": "assistant", "content": f"[Turn stopped] {error}"})
        raise
    finally:
        engine.track_strategy = False
        engine.requests_left = None


async def run_arm(task, arm, workspace, args):
    materialize(task, workspace)
    requests = []

    async def record(response):
        if not response.request.url.path.endswith("/chat/completions"):
            return
        await response.aread()
        body = json.loads(response.request.content)
        try:
            data = response.json()
        except ValueError:
            data = {}
        usage = data.get("usage") or {}
        timings = data.get("timings") or {}
        requests.append({
            "branch": _branch(body["messages"][0]["content"]),
            "status": response.status_code,
            "seconds": round(response.elapsed.total_seconds(), 2),
            "prompt_tokens": usage.get("prompt_tokens"),
            "prompt_tokens_processed": timings.get("prompt_n"),
            "completion_tokens": usage.get("completion_tokens"),
        })

    events = []
    timeout = httpx.Timeout(args.timeout, connect=5)
    async with httpx.AsyncClient(base_url=args.url, timeout=timeout, event_hooks={"response": [record]}) as http:
        server = LlamaClient(http)
        model = args.model or await server.default_model()
        tree = ConversationTree(workspace / ".refl" / "sessions", SYSTEM_PROMPT)
        started = time.monotonic()
        engine = Engine(server, model, WorkspaceTools(workspace), tree, args.max_turns, args.max_tokens,
                        lambda text: events.append([round(time.monotonic() - started, 1), text]), **ARMS[arm],
                        thinking=args.thinking)
        answer = None
        work = (respond_after_stall(engine, task, args.max_turns, args.heat) if args.seeded
                else engine.respond(task.prompt))
        try:
            answer = await asyncio.wait_for(work, args.deadline)
            outcome = "answered"
        except asyncio.TimeoutError:
            outcome = "deadline"
        except Exception as error:  # Record any failure and continue with the next run.
            outcome = f"{type(error).__name__}: {error}"
        seconds = round(time.monotonic() - started, 1)
        tree.save()
        trajectory = analyse(tree)
        for reflection in trajectory["reflection_details"]:
            reflection["distinct"] = await distinct_ideas(server, model, proposals(reflection["message"]),
                                                          args.max_tokens)

    passed, detail = task.check(workspace)
    by_branch = {}
    for request in requests:
        by_branch[request["branch"]] = by_branch.get(request["branch"], 0) + 1
    measured = [request for request in requests if request["branch"] != "judge"]
    return {
        "task": task.name,
        "arm": arm,
        "mode": f"seeded at heat {args.heat}" if args.seeded else "natural",
        "thinking": args.thinking,
        "passed": passed,
        "check": detail,
        "outcome": outcome,
        "answer": (answer or "")[:1000],
        "seconds": seconds,
        "requests": by_branch,
        "prompt_tokens_processed": sum(r["prompt_tokens_processed"] or 0 for r in measured),
        "completion_tokens": sum(r["completion_tokens"] or 0 for r in measured),
        "trajectory": trajectory,
        "session": str(tree.path),
        "workspace": str(workspace),
        "request_log": requests,
        "events": events,
    }


def summarize(results, arms):
    lines = [
        "| Task | Arm | Pass | Outcome | Requests main/fork | Stall at | Roles | Distinct ideas | Seconds "
        "| Prompt tok processed | Completion tok |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run in results:
        requests = run["requests"]
        forks = sum(count for branch, count in requests.items() if branch not in {"main", "judge"})
        stall = run["trajectory"]["first_stall"]
        details = run["trajectory"]["reflection_details"]
        roles = "; ".join(", ".join(detail["roles"]) for detail in details) or "-"
        distinct = ", ".join(f"{detail.get('distinct')}/{len(detail['roles'])}" for detail in details) or "-"
        lines.append(
            f"| {run['task']} | {run['arm']} | {'yes' if run['passed'] else 'no'} | {run['outcome'][:40]} "
            f"| {requests.get('main', 0)}/{forks} | {stall['after_tool_results'] if stall else '-'} "
            f"| {roles} | {distinct} | {run['seconds']} | {run['prompt_tokens_processed']} "
            f"| {run['completion_tokens']} |"
        )
    if "linear" in arms and len(arms) > 1:
        lines += ["", "| Task | Compared with linear | Result |", "| --- | --- | --- |"]
        by_key = {(run["task"], run["arm"], run["repeat"]): run for run in results}
        for (task, arm, repeat), run in by_key.items():
            baseline = by_key.get((task, "linear", repeat))
            if arm == "linear" or baseline is None:
                continue
            if run["passed"] == baseline["passed"]:
                verdict = "both solved" if run["passed"] else "neither solved"
            else:
                verdict = f"{arm} solved, linear failed" if run["passed"] else f"linear solved, {arm} failed"
            lines.append(f"| {task} | {arm} | {verdict} |")
    return "\n".join(lines) + "\n"


async def run(args):
    out = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "refl-bench" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"Results: {out}", flush=True)
    names = [name for name in args.tasks if not args.seeded or TASKS[name].stall]
    results = []
    for repeat in range(args.repeat):
        for index, name in enumerate(names):
            # Alternate the arm order so the server's prompt cache does not always favor the same arm.
            arms = args.arms if (index + repeat) % 2 == 0 else list(reversed(args.arms))
            for arm in arms:
                workspace = out / "workspaces" / f"{name}-{arm}-{repeat + 1}"
                print(f"Running {name} with {arm} (repeat {repeat + 1})", flush=True)
                result = await run_arm(TASKS[name], arm, workspace, args)
                result["repeat"] = repeat + 1
                results.append(result)
                print(f"  {'PASS' if result['passed'] else 'FAIL'} {result['outcome'][:80]} "
                      f"in {result['seconds']} s, requests {result['requests']}", flush=True)
                (out / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False),
                                                  encoding="utf-8")
    summary = summarize(results, args.arms)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary, flush=True)


async def judge_results(args):
    """Count distinct ideas in the reflections of an earlier results directory."""
    runs = json.loads(Path(args.judge, "results.json").read_text(encoding="utf-8"))
    async with httpx.AsyncClient(base_url=args.url, timeout=httpx.Timeout(args.timeout, connect=5)) as http:
        server = LlamaClient(http)
        model = args.model or await server.default_model()
        for run in runs:
            trajectory = run["trajectory"]
            # Results from before the role pool kept the reflection messages under another key.
            messages = ([detail["message"] for detail in trajectory.get("reflection_details", [])]
                        or trajectory.get("reflection_messages", []))
            for message in messages:
                ideas = proposals(message)
                distinct = await distinct_ideas(server, model, ideas, args.max_tokens)
                print(f"{run['task']} {run['arm']} {run.get('repeat')}: {distinct}/{len(ideas)} distinct "
                      f"({', '.join(name for name, _ in ideas)})", flush=True)


def validate():
    """Check that each fixture fails as written and passes with its reference fix."""
    ok = True
    for task in TASKS.values():
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            materialize(task, workspace)
            before, detail_before = task.check(workspace)
            task.reference(workspace)
            after, detail_after = task.check(workspace)
        status = "ok" if not before and after else "BROKEN"
        ok &= status == "ok"
        print(f"{task.name}: {status}\n  before: {detail_before}\n  after:  {detail_after}")
        if task.stall:
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                materialize(task, workspace)
                tree = ConversationTree(workspace / ".refl" / "sessions", SYSTEM_PROMPT, persist=False)
                engine = Engine(None, "model", WorkspaceTools(workspace), tree, 20, 256, lambda text: None,
                                **ARMS["reflect"])
                reason, fired_at = asyncio.run(seed_stall(engine, task))
                still_failing = not task.check(workspace)[0]
            seeded = bool(reason) and fired_at == len(task.stall) - 1 and still_failing
            ok &= seeded
            print(f"  seeded stall: {'ok' if seeded else 'BROKEN'} at step {fired_at}: {reason}")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("LLAMA_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--model", help="model ID (default: first ID from /v1/models)")
    parser.add_argument("--tasks", nargs="+", choices=list(TASKS), default=list(TASKS))
    parser.add_argument("--arms", nargs="+", choices=list(ARMS), default=["linear", "reflect"])
    parser.add_argument("--repeat", type=int, default=1, help="runs per task and arm (default: 1)")
    parser.add_argument("--max-turns", type=int, default=20, help="request budget per task (default: 20)")
    parser.add_argument("--max-tokens", type=int, default=4096, help="output tokens per request (default: 4096)")
    parser.add_argument("--timeout", type=int, default=300, help="seconds per model request (default: 300)")
    parser.add_argument("--deadline", type=int, default=900, help="seconds per task run (default: 900)")
    parser.add_argument("--out", help="results directory (default: a new folder under the system temp directory)")
    parser.add_argument("--seeded", action="store_true",
                        help="start each run from the task's scripted stall (tasks without one are skipped)")
    parser.add_argument("--heat", type=int, choices=range(3), default=0,
                        help="with --seeded, reflection temperature for the seeded stall (default: 0)")
    parser.add_argument("--thinking", choices=THINKING_MODES, default="auto",
                        help="thinking mode as in refl --thinking (default: auto)")
    parser.add_argument("--validate", action="store_true", help="check the fixtures and reference fixes only")
    parser.add_argument("--judge", metavar="RESULTS_DIR",
                        help="count distinct ideas in the reflections of an earlier run and exit")
    args = parser.parse_args()
    if args.validate:
        return validate()
    if args.judge:
        asyncio.run(judge_results(args))
        return 0
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
