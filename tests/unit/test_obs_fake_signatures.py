"""Every hand-written OBS client in the test suite, held to the real one.

CI has no OBS, so each test of the stream, the director or the QA harness talks
to a small fake. The fake stays honest only while it takes the arguments the real
`obsws_python.ReqClient` takes: `save_source_screenshot(scene, fmt, path, w, h,
quality)` in `test_capture_scenes` differed from the real
`(name, img_format, file_path, width, height, quality)` and nothing noticed
(test-double audit 2, fix 4).

Two halves, so a NEW fake cannot slip past:

- every fake in `OBS_FAKES` has parameter names, kinds and defaults equal to
  `ReqClient`'s, derived by `inspect.signature` from the installed library;
- `OBS_FAKES` is the complete list: an AST scan of `tests/` finds every class
  that defines a `ReqClient` method (by `def` or class-body assignment), every
  class with `__getattr__`, and every Mock/`create_autospec` in a file that names
  a `ReqClient` method, and fails if one is missing from it. The scan is a
  guard, not a proof: see `_fakes_in_source` for what it cannot see (a fake built
  by `type()`/`SimpleNamespace`, a Mock in a file that never spells a real method
  name).

The watchdog tests have no OBS fake of their own: they replace
`stream_ctl.make_client` and the `stream_ctl` functions with stubs, so the
watchdog reaches the same `FakeClient` as `test_stream_ctl` or none at all.
"""

import ast
import importlib
import inspect
from pathlib import Path

import obsws_python
import pytest

from tests.unit.obs_real import signature_diffs

TESTS = Path(__file__).resolve().parents[1]

# (module, class) of every test double that stands in for `obsws_python.ReqClient`.
OBS_FAKES = [
    ("tests.unit.test_stream_ctl", "FakeClient"),
    ("tests.unit.test_music_bed", "FakeClient"),
    ("tests.unit.test_capture_scenes", "FakeClient"),
    ("tests.unit.test_director_integration", "_FakeObs"),
    ("tests.unit.test_director_rails", "_FakeObs"),
    ("tests.unit.test_director_restart_scene", "FakeObs"),
    ("tests.unit.test_director_standby_handoff", "FakeObs"),
    ("tests.unit.test_director_clock", "FakeObs"),
    ("tests.unit.test_director_restart_narration", "_Obs"),
    ("tests.unit.test_director_at_most_once", "_Obs"),
]

# (module, class) of test doubles the scan flags by shape only: they stand in for
# a clock, a websocket or `asyncio`, never for the OBS client (`sleep`/`send`/`close`
# share a name with a `ReqClient` method; `_AsyncioProxy` has `__getattr__`).
# Named one by one, so a new OBS fake in the same file is still caught.
NOT_OBS_FAKES = [
    ("tests.unit.test_director_clock", "FakeClock"),
    ("tests.unit.test_liquidation_recorder_loops", "FakeWS"),
    ("tests.unit.test_liquidation_recorder_loops", "_AsyncioProxy"),
    ("tests.unit.test_liquidation_recorder_loops", "Sleeper"),
]


def _fake(module, name):
    return getattr(importlib.import_module(module), name)


def _top_level_bindings(source, name):
    """The module-level statements that bind `name`, in order, as node kinds.

    `getattr(module, name)` sees only the last module-level binding, so a class
    is only what the scan checked if it is the sole one: a second `class`, a
    `def`, an assignment or an import of the same name would redirect it. Names
    bound inside a nested class or function do not count. Bindings under a
    top-level `if`/`try`/`with` are not seen (test modules do not do that)."""
    kinds = []
    for n in ast.parse(source).body:
        if isinstance(n, ast.ClassDef) and n.name == name:
            kinds.append("class")
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            kinds.append("def")
        elif isinstance(n, (ast.Import, ast.ImportFrom)) and any(
            (a.asname or a.name).split(".")[0] == name for a in n.names
        ):
            kinds.append("import")
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(
                isinstance(x, ast.Name) and x.id == name
                for t in targets
                for x in ast.walk(t)
            ):
                kinds.append("assign")
    return kinds



@pytest.mark.parametrize(
    "module,name", OBS_FAKES, ids=[m.split(".")[-1] for m, _ in OBS_FAKES]
)
def test_every_obs_fake_takes_the_arguments_the_real_client_takes(module, name):
    fake = _fake(module, name)
    assert any(
        not m.startswith("_") and callable(v) for m, v in vars(fake).items()
    ), "a fake with no public method pins nothing"
    assert signature_diffs(fake) == {}


def test_the_screenshot_fake_in_the_capture_harness_names_what_the_real_call_names():
    """The case the audit found, spelled out: not just 'equal', but the six
    names the real client has, so a pin that quietly compared nothing is seen."""
    fake = _fake("tests.unit.test_capture_scenes", "FakeClient")
    names = list(inspect.signature(fake.save_source_screenshot).parameters)[1:]
    assert names == [
        "name", "img_format", "file_path", "width", "height", "quality",
    ]
    real = inspect.signature(obsws_python.ReqClient.save_source_screenshot)
    assert names == list(real.parameters)[1:]


_MOCK_FACTORIES = {
    "Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock",
    "create_autospec",
}
_REAL = {m for m in dir(obsws_python.ReqClient) if not m.startswith("_")}


def _fakes_in_source(source, module, real=_REAL):
    """`{(module, name)}` for every OBS stand-in `source` defines.

    A stand-in is one of:

    - a class with a `ReqClient` method, as a `def` OR as a class-body
      assignment (`name = lambda self, ...: ...`, `name: T = f`);
    - a class defining `__getattr__` (answers every method, pins none);
    - a `Mock`/`MagicMock`/`create_autospec(...)` call in a file that also
      names a `ReqClient` method (as an attribute, e.g. `m.set_input_volume`,
      or a string), reported as `<Mock@line>`.

    NOT detected, so say so rather than imply it: a fake built by `type()` or
    `SimpleNamespace`, an alias of an imported mock, a Mock in a file that
    never spells a real method name, and a fake whose method names are all
    assigned dynamically (`setattr`). Mocks are only flagged by that name
    heuristic because other tests use Mock for non-OBS collaborators.
    """
    tree = ast.parse(source)
    found = set()
    mentions_real = any(
        (isinstance(n, ast.Attribute) and n.attr in real)
        or (isinstance(n, ast.Constant) and n.value in real)
        for n in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names = set()
            for n in node.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(n.name)
                elif isinstance(n, ast.Assign):
                    names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    names.add(n.target.id)
            if names & real or "__getattr__" in names:
                found.add((module, node.name))
        elif mentions_real and isinstance(node, ast.Call):
            f = node.func
            called = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if called in _MOCK_FACTORIES:
                found.add((module, f"<Mock@{node.lineno}>"))
    return found


def _obs_fake_classes():
    """Every OBS stand-in in `tests/`, as `(module, name)`."""
    found = set()
    for path in sorted(TESTS.rglob("*.py")):
        if path.name == "obs_real.py":
            continue  # its `_NoSocket` stub has `send`, and is the library's seam
        module = ".".join(path.relative_to(TESTS.parent).with_suffix("").parts)
        found |= _fakes_in_source(path.read_text(), module)
    return found


def test_the_list_of_obs_fakes_is_complete():
    """A new OBS double in any test file must be added to `OBS_FAKES`, which is
    what puts it under the signature pin above."""
    assert _obs_fake_classes() - set(NOT_OBS_FAKES) == set(OBS_FAKES)


# `ReqClient` names that a clock, websocket or `asyncio` stand-in shares by chance
_SHARED_NAMES = {"sleep", "send", "close"}


def test_the_not_obs_exclusions_are_live_and_share_only_chance_names():
    """An exclusion must still match a class the scan flags (no stale entries),
    and the only `ReqClient` names such a class may define are the chance ones
    above: give `FakeClock` a `set_current_program_scene` and this goes red."""
    assert set(NOT_OBS_FAKES) <= _obs_fake_classes()
    for module, name in NOT_OBS_FAKES:
        own = {m for m in vars(_fake(module, name)) if m in _REAL}
        assert own <= _SHARED_NAMES, (module, name, own - _SHARED_NAMES)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("class F:\n    def set_input_volume(self, a): ...\n", {"F"}),
        ("class F:\n    set_input_volume = lambda self, a: None\n", {"F"}),
        ("class F:\n    set_input_volume: object = print\n", {"F"}),
        ("class F:\n    def __getattr__(self, n): return print\n", {"F"}),
        ("class F:\n    def helper(self): ...\n    x = 1\n", set()),
        (
            "from unittest.mock import MagicMock\n"
            "c = MagicMock()\nc.set_input_volume.return_value = 1\n",
            {"<Mock@2>"},
        ),
        (
            "from unittest import mock\n"
            "c = mock.create_autospec(object)\nc.get_scene_list()\n",
            {"<Mock@2>"},
        ),
        # a Mock for something that is not an OBS client stays out
        ("from unittest.mock import MagicMock\nm = MagicMock()\nm.fetch()\n", set()),
    ],
    ids=[
        "def", "lambda-assign", "annassign", "getattr", "clean-class",
        "magicmock", "create_autospec", "unrelated-mock",
    ],
)
def test_the_completeness_scan_sees_every_shape_of_obs_stand_in(source, expected):
    """Each detection of `_fakes_in_source`, proven on a planted snippet: the
    shapes the first version of the scan walked past stay red."""
    assert {n for _, n in _fakes_in_source(source, "m")} == expected


@pytest.mark.parametrize(
    "source,name,expected",
    [
        ("class F:\n    pass\n", "F", ["class"]),
        ("class F:\n    pass\nclass F:\n    pass\n", "F", ["class", "class"]),
        ("class F:\n    pass\ndef F(): ...\n", "F", ["class", "def"]),
        ("class F:\n    pass\nF = object\n", "F", ["class", "assign"]),
        ("class F:\n    pass\nF: type = object\n", "F", ["class", "assign"]),
        ("class F:\n    pass\nfrom m import X as F\n", "F", ["class", "import"]),
        ("class F:\n    pass\nimport F\n", "F", ["class", "import"]),
        # nested definitions are not module attributes: not a binding of `F`
        ("class G:\n    class F:\n        pass\n", "F", []),
        ("def g():\n    class F:\n        pass\n", "F", []),
        ("class F:\n    pass\n", "G", []),
    ],
    ids=[
        "once", "redefined", "def-after", "assign-after", "annassign-after",
        "import-as-after", "import-after", "nested-class", "local-class", "absent",
    ],
)
def test_the_binding_scan_sees_every_way_a_name_can_be_rebound(
    source, name, expected
):
    """`_fake()` and the scan key a double by `(module, name)`, so a rebinding
    of the name hides an earlier class behind the last one (review mutant M3)."""
    assert _top_level_bindings(source, name) == expected


@pytest.mark.parametrize(
    "module,name",
    OBS_FAKES + NOT_OBS_FAKES,
    ids=lambda v: v.split(".")[-1],
)
def test_every_listed_double_is_defined_exactly_once_in_its_module(module, name):
    """`(module, class)` resolves only the last binding of a name. A second class
    of that name, defined earlier in the module (say a `FakeClock` with a real
    `set_input_volume` above the real `FakeClock`), or a later `def`, assignment
    or import of the name, would pass the signature pin and the exclusion check
    while a different class went unchecked."""
    path = importlib.import_module(module).__file__
    source = Path(path).read_text(encoding="utf-8")
    assert _top_level_bindings(source, name) == ["class"], (module, name)
