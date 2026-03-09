import json
import tempfile
from pathlib import Path

from src.state import State


def test_fresh_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        s = State(str(Path(tmpdir) / "state.json"))
        assert s.build_id is None


def test_set_and_get_build_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = str(Path(tmpdir) / "state.json")
        s = State(state_file)
        s.build_id = "1.2.3.4"
        assert s.build_id == "1.2.3.4"

        with open(state_file) as f:
            data = json.load(f)
        assert data["build_id"] == "1.2.3.4"
        assert "last_updated" in data


def test_state_persists_across_instances():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = str(Path(tmpdir) / "state.json")
        State(state_file).build_id = "5.6.7"
        assert State(state_file).build_id == "5.6.7"


def test_creates_parent_directories():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = str(Path(tmpdir) / "nested" / "deep" / "state.json")
        s = State(state_file)
        s.build_id = "test"
        assert Path(state_file).exists()


def test_manifest_gid_initially_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        s = State(str(Path(tmpdir) / "state.json"))
        assert s.manifest_gid is None


def test_set_and_get_manifest_gid():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = str(Path(tmpdir) / "state.json")
        s = State(state_file)
        s.manifest_gid = "8234567890123456789"
        assert s.manifest_gid == "8234567890123456789"


def test_manifest_gid_persists_across_instances():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = str(Path(tmpdir) / "state.json")
        State(state_file).manifest_gid = "8234567890123456789"
        assert State(state_file).manifest_gid == "8234567890123456789"


def test_file_hashes_initially_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        s = State(str(Path(tmpdir) / "state.json"))
        assert s.file_hashes is None


def test_set_build_persists_both_fields():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = str(Path(tmpdir) / "state.json")
        s = State(state_file)
        hashes = {"scripts/a.vdata_c": "abc123", "scripts/b.vdata_c": "def456"}
        s.set_build("1.2.3.4", hashes)
        assert s.build_id == "1.2.3.4"
        assert s.file_hashes == hashes

        with open(state_file) as f:
            data = json.load(f)
        assert data["build_id"] == "1.2.3.4"
        assert data["file_hashes"] == hashes
        assert "last_updated" in data


def test_set_build_with_manifest_gid():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = str(Path(tmpdir) / "state.json")
        s = State(state_file)
        s.set_build("1.2.3.4", {"a": "b"}, manifest_gid="8234567890123456789")
        assert s.build_id == "1.2.3.4"
        assert s.manifest_gid == "8234567890123456789"

        with open(state_file) as f:
            data = json.load(f)
        assert data["manifest_gid"] == "8234567890123456789"


def test_set_build_without_manifest_gid_preserves_existing():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = str(Path(tmpdir) / "state.json")
        s = State(state_file)
        s.manifest_gid = "8234567890123456789"
        s.set_build("1.2.3.4", {"a": "b"})  # no manifest_gid kwarg
        assert s.manifest_gid == "8234567890123456789"


def test_set_build_persists_across_instances():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = str(Path(tmpdir) / "state.json")
        hashes = {"scripts/a.vdata_c": "deadbeef"}
        State(state_file).set_build("9.9.9", hashes)
        s2 = State(state_file)
        assert s2.build_id == "9.9.9"
        assert s2.file_hashes == hashes
