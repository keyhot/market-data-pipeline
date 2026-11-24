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


def _store_names(target):
    """Names a binding target (re)binds: walk only the containers that unpack
    (`Tuple`, `List`, `Starred`) down to `Name` nodes. `F.x = 0`, `REG[F] = 1`,
    `F.n += 1` and `del F.x` use `F` without rebinding it, and a comprehension
    variable or walrus inside a subscript is not a binding either, so the walk
    stops at `Attribute` and `Subscript`."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*map(_store_names, target.elts)) if target.elts else set()
    if isinstance(target, ast.Starred):
        return _store_names(target.value)
    return set()


def _walrus_names(node):
    """Names bound by `(x := ...)` inside `node`, not looking into a `lambda`,
    `def` or `class` (their walrus binds their own scope)."""
    names = set()
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(
            n, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        if isinstance(n, ast.NamedExpr):
            names.add(n.target.id)
        stack.extend(ast.iter_child_nodes(n))
    return names


def _top_level_bindings(source, name):
    """The module-level statements that bind `name`, as node kinds, in source
    order (a `try` is walked body, handlers, else, finally).

    `getattr(module, name)` sees only the last module-level binding, so a class
    is only what the scan checked if it is the sole one: a second `class`, a
    `def`, an assignment, an import, a `for`/`with`/`except` target, a walrus or
    a `del` of the same name would redirect it. Only the NAME being bound counts:
    `F.x = 0`, `REG[F] = 1`, `F.n += 1` and a bare `F: type` do not. Names bound
    inside a nested class or function do not count either. Compound statements
    (`if`/`try`/`for`/`while`/`with`) are entered, since a module-level `if`
    binds at module level. A `from m import *` is reported (it may rebind
    anything, and the scan cannot tell), which fails loudly in a listed module.

    Out of scope, deliberately (test modules do not do these, and no static scan
    sees them all): `globals()[name] = ...`, `setattr(sys.modules[...])`, `exec`,
    `global name` in a class body, `match` capture patterns (bodies of `case`
    arms are walked), a walrus in a decorator, default, annotation, class base
    or `except` type, `type F = ...` (3.12 syntax; the project targets 3.11), a
    decorator on `class F` that returns something else, and a binding made in
    another module that this one star-imports from (the star import itself is
    what gets reported)."""
    kinds = []

    def walk(stmts):
        for n in stmts:
            if isinstance(n, ast.ClassDef):
                if n.name == name:
                    kinds.append("class")
                continue
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if n.name == name:
                    kinds.append("def")
                continue
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    if a.name == "*":
                        kinds.append("star-import")
                    elif (a.asname or a.name).split(".")[0] == name:
                        kinds.append("import")
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                if isinstance(n, ast.AnnAssign) and n.value is None:
                    pass  # a bare annotation binds nothing
                else:
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    if any(name in _store_names(t) for t in targets):
                        kinds.append("assign")
            elif isinstance(n, ast.Delete):
                if any(name in _store_names(t) for t in n.targets):
                    kinds.append("del")
            elif isinstance(n, (ast.For, ast.AsyncFor)):
                if name in _store_names(n.target):
                    kinds.append("for")
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                if any(
                    i.optional_vars is not None
                    and name in _store_names(i.optional_vars)
                    for i in n.items
                ):
                    kinds.append("with")
            # a walrus in this statement's own expressions (not in nested bodies)
            heads = [
                c
                for c in ast.iter_child_nodes(n)
                if not isinstance(c, (ast.stmt, ast.ExceptHandler, ast.match_case))
            ]
            if any(name in _walrus_names(h) for h in heads):
                kinds.append("walrus")
            walk(getattr(n, "body", []) or [])
            for h in getattr(n, "handlers", []) or []:
                if h.name == name:
                    kinds.append("except")
                walk(h.body)
            for c in getattr(n, "cases", []) or []:
                walk(c.body)
            walk(getattr(n, "orelse", []) or [])
            walk(getattr(n, "finalbody", []) or [])

    walk(ast.parse(source).body)
    return kinds


def _class_defs(source, name):
    """How many `class <name>` statements `source` holds, at any depth. A nested
    one (`def test_x(): class FakeClock: ...`) is no module attribute, but
    `_fakes_in_source` walks the whole tree and folds it into the same
    `(module, name)` key, so the signature pin would never look at it."""
    return sum(
        isinstance(n, ast.ClassDef) and n.name == name
        for n in ast.walk(ast.parse(source))
    )


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
        found |= _fakes_in_source(path.read_bytes(), module)
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
        ("class F:\n    pass\nfrom m import F\n", "F", ["class", "import"]),
        ("class F:\n    pass\nF += 1\n", "F", ["class", "assign"]),
        ("class F:\n    pass\nF, G = 1, 2\n", "F", ["class", "assign"]),
        ("class F:\n    pass\n[G, *F] = 1, 2\n", "F", ["class", "assign"]),
        ("class F:\n    pass\nasync def F(): ...\n", "F", ["class", "def"]),
        ("class F:\n    pass\ndel F\n", "F", ["class", "del"]),
        # control flow at module level: the same rebinding, one level down
        ("class F:\n    pass\nif 1:\n    F = 0\n", "F", ["class", "assign"]),
        ("if 1:\n    class F:\n        pass\n", "F", ["class"]),
        (
            "class F:\n    pass\ntry:\n    pass\nexcept E:\n    F = 0\n",
            "F",
            ["class", "assign"],
        ),
        (
            "class F:\n    pass\ntry:\n    pass\nexcept E as F:\n    pass\n",
            "F",
            ["class", "except"],
        ),
        ("class F:\n    pass\ntry:\n    pass\nfinally:\n    F = 0\n", "F",
         ["class", "assign"]),
        ("class F:\n    pass\nfor F in x:\n    pass\n", "F", ["class", "for"]),
        ("class F:\n    pass\nfor i in x:\n    F = i\n", "F", ["class", "assign"]),
        ("class F:\n    pass\nwhile x:\n    F = 0\n", "F", ["class", "assign"]),
        ("class F:\n    pass\nwith a as F:\n    pass\n", "F", ["class", "with"]),
        ("class F:\n    pass\nwith a as (G, F):\n    pass\n", "F",
         ["class", "with"]),
        ("class F:\n    pass\nwith a as G:\n    F = 0\n", "F", ["class", "assign"]),
        ("class F:\n    pass\n(F := object)\n", "F", ["class", "walrus"]),
        ("class F:\n    pass\nif (F := 1):\n    pass\n", "F", ["class", "walrus"]),
        ("class F:\n    pass\nfrom m import *\n", "F", ["class", "star-import"]),
        ("class F:\n    pass\nimport F.sub\n", "F", ["class", "import"]),
        ("class F:\n    pass\nmatch x:\n    case 1:\n        F = 0\n", "F",
         ["class", "assign"]),
        (
            "try:\n    class F:\n        pass\nexcept E as F:\n    pass\n"
            "else:\n    F = 1\nfinally:\n    F = 0\n",
            "F",
            ["class", "except", "assign", "assign"],
        ),
        # naming the fake without rebinding it is legitimate test code
        ("class F:\n    pass\nF.x = 0\n", "F", ["class"]),
        ("class F:\n    pass\nREG[F] = 1\n", "F", ["class"]),
        ("class F:\n    pass\nF.n += 1\n", "F", ["class"]),
        ("class F:\n    pass\nF.a.b = 1\n", "F", ["class"]),
        ("class F:\n    pass\nx = F()\nG = F\n", "F", ["class"]),
        ("class F:\n    pass\ndel F.x\n", "F", ["class"]),
        ("class F:\n    pass\ndel REG[F]\n", "F", ["class"]),
        ("class F:\n    pass\nREG[tuple(F for F in xs)] = 1\n", "F", ["class"]),
        ("class F:\n    pass\nREG[lambda: (F := 1)] = 1\n", "F", ["class"]),
        ("class F:\n    pass\nimport F.sub as G\nfrom m import F as G\n", "F",
         ["class"]),
        ("class F:\n    pass\nF: type\n", "F", ["class"]),
        ("class F:\n    pass\nfor i in F:\n    pass\n", "F", ["class"]),
        ("class F:\n    pass\nwith F() as g:\n    pass\n", "F", ["class"]),
        ("class F:\n    pass\nimport m\nm.F = 1\n", "F", ["class"]),
        # nested definitions are not module attributes: not a binding of `F`
        ("class G:\n    class F:\n        pass\n", "F", []),
        ("def g():\n    class F:\n        pass\n", "F", []),
        ("def g():\n    F = 0\n    for F in x: ...\n    (F := 1)\n", "F", []),
        ("x = lambda: (F := 1)\n", "F", []),
        ("class G:\n    F = 0\n", "F", []),
        ("class F:\n    pass\n", "G", []),
    ],
    ids=[
        "once", "redefined", "def-after", "assign-after", "annassign-after",
        "import-as-after", "import-after", "from-import-after", "augassign-after",
        "tuple-after", "starred-after", "async-def-after", "del-after",
        "if-assign", "if-class", "except-assign", "except-as", "finally-assign",
        "for-target", "for-body-assign", "while-assign", "with-as", "with-tuple-as",
        "with-body-assign", "walrus", "walrus-in-if", "star-import",
        "import-dotted", "match-case-assign", "try-order",
        "attr-assign-ok", "subscript-assign-ok", "attr-augassign-ok",
        "attr-chain-ok", "use-ok", "del-attr-ok", "del-subscript-ok",
        "comprehension-target-ok", "lambda-walrus-in-target-ok",
        "import-other-ok", "bare-annotation-ok", "for-iter-ok", "with-expr-ok",
        "module-attr-ok",
        "nested-class", "local-class", "local-bindings", "lambda-walrus",
        "class-attr", "absent",
    ],
)
def test_the_binding_scan_sees_every_way_a_name_can_be_rebound(
    source, name, expected
):
    """`_fake()` and the scan key a double by `(module, name)`, so a rebinding
    of the name hides an earlier class behind the last one (review mutant M3)."""
    assert _top_level_bindings(source, name) == expected


@pytest.mark.parametrize(
    "source,name,expected",
    [
        ("class F:\n    pass\n", "F", 1),
        ("class F:\n    pass\nclass F:\n    pass\n", "F", 2),
        ("class G:\n    class F:\n        pass\nclass F:\n    pass\n", "F", 2),
        ("def test_x():\n    class F:\n        pass\nclass F:\n    pass\n", "F", 2),
        ("if 1:\n    class F:\n        pass\n", "F", 1),
        ("class G:\n    pass\n", "F", 0),
    ],
    ids=["once", "twice", "nested-in-class", "nested-in-def", "under-if", "absent"],
)
def test_the_class_count_sees_a_same_name_class_at_any_depth(source, name, expected):
    """A nested `class FakeClock` is no module binding, but the OBS scan walks
    the whole tree and files it under the same `(module, name)`, so the
    signature pin (which checks only the module-level class) would skip it."""
    assert _class_defs(source, name) == expected


def test_the_scans_read_a_source_with_a_byte_order_mark():
    """`read_text(encoding="utf-8")` would leave a BOM in the text and
    `ast.parse` would raise; the guard hands `ast.parse` the raw bytes."""
    src = b"\xef\xbb\xbfclass F:\n    pass\n"
    assert _top_level_bindings(src, "F") == ["class"]
    assert _class_defs(src, "F") == 1
    assert _fakes_in_source(src, "m") == set()


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
    while a different class went unchecked. A same-name class nested in a
    function or class is no binding, but the scan files it under the same key
    and the pin never looks at it, so it is counted too."""
    source = Path(importlib.import_module(module).__file__).read_bytes()
    assert _top_level_bindings(source, name) == ["class"], (module, name)
    assert _class_defs(source, name) == 1, (module, name)
