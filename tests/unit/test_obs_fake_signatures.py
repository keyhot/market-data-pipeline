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
  that defines a `ReqClient` method and fails if one is missing from it.

The watchdog tests have no OBS fake of their own: they replace
`stream_ctl.make_client` and the `stream_ctl` functions with stubs, so the
watchdog reaches the same `FakeClient` as `test_stream_ctl` or none at all.
"""

import ast
import importlib
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
]


def _fake(module, name):
    return getattr(importlib.import_module(module), name)


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
    names = list(fake.save_source_screenshot.__code__.co_varnames[1:7])
    assert names == [
        "name", "img_format", "file_path", "width", "height", "quality",
    ]
    assert names == list(
        obsws_python.ReqClient.save_source_screenshot.__code__.co_varnames[1:7]
    )


def _obs_fake_classes():
    """`(module, class)` for every test class defining a `ReqClient` method."""
    real = {m for m in dir(obsws_python.ReqClient) if not m.startswith("_")}
    found = set()
    for path in sorted(TESTS.rglob("*.py")):
        if path.name == "obs_real.py":
            continue  # its `_NoSocket` stub has `send`, and is the library's seam
        module = ".".join(path.relative_to(TESTS.parent).with_suffix("").parts)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name in real
                for n in node.body
            ):
                found.add((module, node.name))
    return found


def test_the_list_of_obs_fakes_is_complete():
    """A new OBS double in any test file must be added to `OBS_FAKES`, which is
    what puts it under the signature pin above."""
    assert _obs_fake_classes() == set(OBS_FAKES)
