"""S19 #54: no `assert` statement outside tests/ -- `python -O` deletes them.

A holdout wall or a statistic allowlist written as `assert` vanishes under
`-O` and nothing says so (the class KI-078 fixed for `rect_from_quad`).  Two
things are pinned here:

1. an AST pin: no `assert` statement in any tracked `.py` outside `tests/`
   (the pathspec of the S19 grep), except the explicit ALLOWLIST below;
2. for every converted guard, a child interpreter started with `-O` that
   still raises `AssertionError` on a breaching input, and still passes on a
   holding one.  The children use synthetic input only (the :609 case
   writes two tiny fake result JSONs into a fresh temp dir it removes at exit);
   no script CLI, archive file, database or network is touched.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# path -> (number of `assert` statements tolerated, reason).  A script whose
# pre-registration note forbids a code edit goes here instead of being edited
# (S19 #54 freeze rule).  The count is exact: a new assert in a listed file
# fails, and a stale entry (count no longer matches) fails too.
#
# Empty on purpose.  All seven scripts were read against their notes
# (r3b-preregistration rule 14 and Amendment 1, e15c / e15b / e16 freeze
# paragraphs, e17, code-review-e13): none forbids a code edit; the freeze
# paragraphs require an amendment for a changed parameter, window, threshold
# or allowlist, and this change leaves every one of them byte-identical.
ALLOWLIST: dict[str, tuple[int, str]] = {}


def assert_lines(source: str) -> list[int]:
    """Line numbers of every `assert` statement in `source` (AST, not text)."""
    return sorted(n.lineno for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Assert))


def tracked_python_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", "*.py", ":!tests/**"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    return sorted(line for line in out.splitlines() if line)


def violations(found: dict[str, list[int]], allowlist: dict[str, tuple[int, str]]) -> list[str]:
    problems = []
    for path, lines in sorted(found.items()):
        allowed = allowlist.get(path, (0, ""))[0]
        if len(lines) != allowed:
            problems.append(f"{path}: assert at lines {lines} (allowlisted: {allowed})")
    for path, (allowed, reason) in sorted(allowlist.items()):
        if allowed and path not in found:
            problems.append(f"{path}: allowlist entry is stale, no assert left ({reason})")
        if not reason.strip():
            problems.append(f"{path}: allowlist entry has no reason")
    return problems


def test_no_bare_assert_outside_tests_except_the_allowlist():
    files = tracked_python_files()
    # the pathspec must not be vacuous: scripts/ and model/ are in scope
    if len(files) < 50 or "scripts/e13_diagnostics.py" not in files or "model/cross_section.py" not in files:
        pytest.fail(f"git ls-files pathspec found {len(files)} files and missed the guarded scripts")
    found = {}
    for path in files:
        lines = assert_lines((REPO / path).read_text(encoding="utf-8"))
        if lines:
            found[path] = lines
    problems = violations(found, ALLOWLIST)
    if problems:
        pytest.fail("`assert` is stripped by python -O; raise instead:\n  " + "\n  ".join(problems))


def test_the_pin_sees_asserts_and_honours_an_exact_count_allowlist():
    src = "x = 1\n# assert nope\ns = 'assert nope'\nif x:\n    assert x, 'm'\nassert (x ==\n        1)\n"
    assert assert_lines(src) == [5, 6]            # statements only; comments and strings are not
    assert assert_lines("raise AssertionError('m')\n") == []
    found = {"scripts/a.py": [5, 6]}
    assert violations(found, {}) == ["scripts/a.py: assert at lines [5, 6] (allowlisted: 0)"]
    assert violations(found, {"scripts/a.py": (2, "frozen: note forbids an edit")}) == []
    assert violations(found, {"scripts/a.py": (1, "r")})                       # a new assert still fails
    assert violations({}, {"scripts/a.py": (2, "r")})                          # stale entry fails
    assert violations(found, {"scripts/a.py": (2, " ")})                       # no reason fails


# ---------------------------------------------------------------- -O cases --

def _indent(code: str, n: int = 4) -> str:
    return textwrap.indent(textwrap.dedent(code).strip("\n"), " " * n)


def _child_source(setup: str, ok: str, bads: list[str]) -> str:
    parts = [
        "import sys",
        "if __debug__:",
        "    sys.exit(97)   # not running under -O: the test would prove nothing",
        textwrap.dedent(setup).strip("\n"),
        textwrap.dedent(ok).strip("\n"),
        "print('HOLDS-OK')",
    ]
    for bad in bads:
        parts += [
            "try:",
            _indent(bad),
            "except AssertionError as exc:",
            "    print('RAISED:', exc)",
            "else:",
            "    print('BREACH-PASSED-SILENTLY')",
            "    sys.exit(98)",
        ]
    return "\n".join(parts) + "\n"


def run_under_O(setup: str, ok: str, bads: list[str]) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "MDP_TEST_DATABASE_URL")}
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), str(REPO / "scripts")])
    return subprocess.run(
        [sys.executable, "-O", "-c", _child_source(setup, ok, bads)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )


PANEL = 'pd.DataFrame({{"a": [1.0]}}, index=pd.DatetimeIndex(["{day}"], tz="UTC"))'
IDX = 'pd.DataFrame({{"close": [1.0, 1.0]}}, index=pd.DatetimeIndex({days}, tz="UTC"))'
KEYS_OK = "{k: 0 for k in %s}"

# id -> (setup, holding call, [breaching calls], regex every breach message must match)
CASES = {
    "e13_diagnostics:96 panel reaches the holdout": (
        "import pandas as pd\nimport e13_diagnostics as e",
        "e._refuse_holdout_panels((('close', %s),))" % PANEL.format(day="2025-12-31"),
        ["e._refuse_holdout_panels((('close', %s),))" % PANEL.format(day="2026-01-01")],
        r"close panel reaches the 2026 holdout",
    ),
    "e13_diagnostics:128 rebuild must reproduce stage B": (
        "import e13_diagnostics as e",
        "e._require_reproduction({'gross_bps': 1.0}, {'gross_bps': 1.0})",
        ["e._require_reproduction({'gross_bps': 1.0}, {'gross_bps': 2.0})"],
        r"rebuild does not reproduce stage B",
    ),
    "e13_diagnostics:146 funding timing self-check": (
        "import e13_diagnostics as e",
        "e.pinned_funding_checks()",
        ["e.forward_funding = lambda f: f * 2\ne.pinned_funding_checks()"],
        r"RAISED: 0\.\d+$",                       # the message is the held value itself
    ),
    "e13_diagnostics:151 funding sign self-check": (
        "import pandas as pd\nimport e13_diagnostics as e",
        "e.pinned_funding_checks()",
        ["e.portfolio_pnl = lambda *a, **k: pd.DataFrame({'funding': [0.001]})\ne.pinned_funding_checks()"],
        r"funding sign",
    ),
    "e13_diagnostics:165 raw funding event reaches the holdout": (
        "import pandas as pd\nimport e13_diagnostics as e",
        "e._refuse_holdout_events(pd.DataFrame({'time': pd.to_datetime(['2025-12-31 23:59:59'], utc=True)}))",
        ["e._refuse_holdout_events(pd.DataFrame({'time': pd.to_datetime(['2026-01-01 00:00:00'], utc=True)}))"],
        r"a raw funding event reaches the 2026 holdout",
    ),
    "e17_a0_report:106 bars reach the scoring cutoff": (
        "import pandas as pd\nimport e17_a0_report as r",
        "r._require_before_scoring_cutoff(pd.DataFrame({'timestamp': ['2025-12-31 23:45']}))",
        ["r._require_before_scoring_cutoff(pd.DataFrame({'timestamp': ['2025-12-31 23:45', '2026-01-01 00:00']}))"],
        r"scoring cutoff",
    ),
    "r3b_stage_a:43 bars outside the development year": (
        "import pandas as pd\nfrom scripts import r3b_stage_a as sa",
        "sa._require_dev_window(%s)" % IDX.format(days='["2024-01-01 00:00", "2024-12-31 23:59"]'),
        ["sa._require_dev_window(%s)" % IDX.format(days='["2024-01-01 00:00", "2025-01-01 00:00"]'),
         "sa._require_dev_window(%s)" % IDX.format(days='["2023-12-31 23:59", "2024-06-01 00:00"]')],
        r"development year",
    ),
    "r3b_stage_b:55 bar before the verdict window": (
        "import pandas as pd\nfrom scripts import r3b_stage_b as sb",
        "sb._require_verdict_window(%s)" % IDX.format(days='["2025-01-01 00:00", "2026-09-27 23:59"]'),
        ["sb._require_verdict_window(%s)" % IDX.format(days='["2024-12-31 23:59", "2025-06-01 00:00"]')],
        r"starts before the verdict window",
    ),
    "r3b_stage_b:56 bar at or after the verdict window end": (
        "import pandas as pd\nfrom scripts import r3b_stage_b as sb",
        "sb._require_verdict_window(%s)" % IDX.format(days='["2025-01-01 00:00", "2026-09-27 23:59"]'),
        ["sb._require_verdict_window(%s)" % IDX.format(days='["2025-01-01 00:00", "2026-09-28 00:00"]')],
        r"reaches the verdict window end",
    ),
    "trend_report:321 stage A statistic outside the allowlist": (
        "import trend_report as m",
        "m._require_allowlisted({k: 0 for k in m.STAGE_A_ALLOWLIST}, m.STAGE_A_ALLOWLIST, 'stage A')",
        ["m._require_allowlisted({**{k: 0 for k in m.STAGE_A_ALLOWLIST}, 'leaked_sharpe': 1.0}, "
         "m.STAGE_A_ALLOWLIST, 'stage A')"],
        r"stage A statistic keys outside the allowlist: \{'leaked_sharpe'\}",
    ),
    "vol_managed_report:235 stage A statistic outside the allowlist": (
        "import vol_managed_report as m",
        "m._require_allowlisted({k: 0 for k in m.STAGE_A_ALLOWLIST}, m.STAGE_A_ALLOWLIST, 'stage A')",
        ["m._require_allowlisted({**{k: 0 for k in m.STAGE_A_ALLOWLIST}, 'leaked_sharpe': 1.0}, "
         "m.STAGE_A_ALLOWLIST, 'stage A')"],
        r"stage A statistic keys outside the allowlist: \{'leaked_sharpe'\}",
    ),
    "vol_target_report:181 stage A0 statistic outside the allowlist": (
        "import vol_target_report as m",
        "m._require_allowlisted({k: 0 for k in m.A0_ALLOWLIST}, m.A0_ALLOWLIST, 'stage A0')",
        ["m._require_allowlisted({**{k: 0 for k in m.A0_ALLOWLIST}, 'leaked_sharpe': 1.0}, "
         "m.A0_ALLOWLIST, 'stage A0')"],
        r"stage A0 statistic keys outside the allowlist: \{'leaked_sharpe'\}",
    ),
    "vol_target_report:450 stage A statistic outside the allowlist": (
        "import vol_target_report as m",
        "m._require_allowlisted({k: 0 for k in m.STAGE_A_ALLOWLIST}, m.STAGE_A_ALLOWLIST, 'stage A')",
        ["m._require_allowlisted({**{k: 0 for k in m.STAGE_A_ALLOWLIST}, 'leaked_sharpe': 1.0}, "
         "m.STAGE_A_ALLOWLIST, 'stage A')"],
        r"stage A statistic keys outside the allowlist: \{'leaked_sharpe'\}",
    ),
    "vol_target_report:642 stage B statistic outside the allowlist": (
        "import vol_target_report as m",
        "m._require_allowlisted({k: 0 for k in m.STAGE_B_ALLOWLIST}, m.STAGE_B_ALLOWLIST, 'stage B')",
        ["m._require_allowlisted({**{k: 0 for k in m.STAGE_B_ALLOWLIST}, 'leaked_sharpe': 1.0}, "
         "m.STAGE_B_ALLOWLIST, 'stage B')"],
        r"stage B statistic keys outside the allowlist: \{'leaked_sharpe'\}",
    ),
    "vol_target_report:609 score_pair scores exactly one pair": (
        "import json, tempfile\nfrom pathlib import Path\nimport vol_target_report as m\n"
        "import atexit, shutil\nrd = Path(tempfile.mkdtemp())\natexit.register(shutil.rmtree, rd, True)\n"
        "(rd / 'e15c_stage_a0.json').write_text(json.dumps({'verdict': 'contaminated', "
        "'extra': {'size_gate_ok_on_chosen_branch': True}}))\n"
        "(rd / 'e15c_stage_a.json').write_text(json.dumps({'verdict': 'VALID', "
        "'extra': {'branch': 'contaminated'}}))",
        # two symbols pass the guard; the (nonexistent) archive root then fails some other way
        "try:\n    m.run_stage_b(Path('/nonexistent'), symbols=('BTCUSDT', 'ETHUSDT'), results_dir=rd)\n"
        "except AssertionError:\n    sys.exit(99)\nexcept Exception:\n    pass",
        ["m.run_stage_b(Path('/nonexistent'), symbols=('BTCUSDT', 'ETHUSDT', 'SOLUSDT'), results_dir=rd)"],
        r"score_pair scores exactly one pair",
    ),
    "model/cross_section:283 non-finite ridge beta": (
        "import numpy as np\nimport pandas as pd\nimport model.cross_section as cs\n"
        "refit = pd.Timestamp('2021-05-01', tz='UTC')",
        "cs._require_finite_beta(np.array([0.1, -0.2]), refit)",
        ["cs._require_finite_beta(np.array([0.1, np.nan]), refit)",
         "cs._require_finite_beta(np.array([np.inf, 0.0]), refit)"],
        r"non-finite ridge beta at refit 2021-05-01",
    ),
}


@pytest.mark.parametrize("case", list(CASES), ids=lambda c: c.split(" ")[0])
def test_the_guard_still_raises_under_python_O(case):
    setup, ok, bads, message = CASES[case]
    r = run_under_O(setup, ok, bads)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "HOLDS-OK" in r.stdout, out                  # no behaviour change when the condition holds
    assert "BREACH-PASSED-SILENTLY" not in r.stdout, out
    raised = [line for line in r.stdout.splitlines() if line.startswith("RAISED:")]
    assert len(raised) == len(bads), out
    for line in raised:
        assert re.search(message, line), line


def test_the_O_harness_would_notice_a_guard_that_does_not_raise():
    r = run_under_O("", "pass", ["pass"])
    assert r.returncode == 98 and "BREACH-PASSED-SILENTLY" in r.stdout
    r = run_under_O("", "pass", ["assert False, 'stripped by -O'"])      # the pre-change form
    assert r.returncode == 98 and "RAISED" not in r.stdout
