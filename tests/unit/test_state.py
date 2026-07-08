from storage.state import load_state, save_state


def test_load_missing_file_returns_empty(tmp_path):
    assert load_state(tmp_path / "nope.json") == {}


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = {"ticker:AAPL:1d": "2026-07-08T12:00:00+00:00"}

    save_state(path, state)

    assert load_state(path) == state


def test_load_corrupted_file_returns_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")

    assert load_state(path) == {}


def test_load_non_mapping_returns_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]")

    assert load_state(path) == {}


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"

    save_state(path, {"a": "b"})

    assert load_state(path) == {"a": "b"}
