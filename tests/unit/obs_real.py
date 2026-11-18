"""The real obsws_python client, for the OBS test doubles to be held to.

CI has no OBS, so every unit test of the stream talks to a hand-written double.
A double laxer than the client it stands in for is how two things stayed green:
a `set_input_volume` that took dB second where the real client takes a
multiplier second, and a `get_scene_item_id` that answered for a source that was
not in the scene (test-double audit 2, F1 and F2).

Importing the library opens no connection; `ReqClient.__init__` is never run.
"""

import inspect

import obsws_python
from obsws_python.error import OBSSDKRequestError

__all__ = ["OBSSDKRequestError", "signature_diffs", "wire"]


def _params(fn):
    """`(name, kind, default)` for every parameter after `self`."""
    return [
        (p.name, p.kind, p.default)
        for p in list(inspect.signature(fn).parameters.values())[1:]
    ]


def signature_diffs(fake_cls) -> dict:
    """What `fake_cls` gets wrong about the real `ReqClient`, by method name.

    Every public callable on the fake must exist on the real client with the
    same parameter names, kinds and defaults: `{name: (fake_params, real_params)}`
    for a mismatch, and `{name: (fake_params, None)}` for a public method the
    real client does not have at all.
    """
    diffs = {}
    for name, member in vars(fake_cls).items():
        if name.startswith("_") or not callable(member):
            continue
        real = getattr(obsws_python.ReqClient, name, None)
        fake_params = _params(member)
        if real is None:
            diffs[name] = (fake_params, None)
        elif _params(real) != fake_params:
            diffs[name] = (fake_params, _params(real))
    return diffs


def wire(method: str, *args, **kwargs) -> tuple[str, dict]:
    """`(requestType, requestData)` the REAL `ReqClient.<method>` would send.

    Runs the library's own method body against a stand-in whose `send` records
    instead of writing to a socket, so nothing the library does to the
    arguments (positional binding, defaults, keeping a `None` as a JSON null)
    is re-implemented here.
    """
    sent = []

    class _NoSocket:
        def send(self, param, data=None, raw=False):
            sent.append((param, data))

    getattr(obsws_python.ReqClient, method)(_NoSocket(), *args, **kwargs)
    (request,) = sent
    return request
