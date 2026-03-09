import tempfile
from pathlib import Path

import pytest

from src.downloader import SteamDownloader


class TestGetManifestGid:
    def _make_downloader(self):
        return SteamDownloader(app_id=1422450, depot_id=1422456, branch="public")

    def test_parses_gid_from_stdout(self, tmp_path, monkeypatch):
        dl = self._make_downloader()
        monkeypatch.setattr(dl, "_run_depot_downloader", lambda cmd, timeout: (
            "Got depot key for 1422456\nManifest 8234567890123456789 @ 2024-01-01\n"
        ))
        assert dl.get_manifest_gid(str(tmp_path)) == "8234567890123456789"

    def test_falls_back_to_manifest_file(self, tmp_path, monkeypatch):
        dl = self._make_downloader()
        monkeypatch.setattr(dl, "_run_depot_downloader", lambda cmd, timeout: "no gid here")
        (tmp_path / "depot_1422456_9999888877776666.manifest").touch()
        assert dl.get_manifest_gid(str(tmp_path)) == "9999888877776666"

    def test_stdout_takes_priority_over_file(self, tmp_path, monkeypatch):
        dl = self._make_downloader()
        monkeypatch.setattr(dl, "_run_depot_downloader", lambda cmd, timeout: (
            "Manifest 1111111111111111 @ 2024-01-01\n"
        ))
        (tmp_path / "depot_1422456_9999999999999999.manifest").touch()
        assert dl.get_manifest_gid(str(tmp_path)) == "1111111111111111"

    def test_raises_when_no_gid_available(self, tmp_path, monkeypatch):
        dl = self._make_downloader()
        monkeypatch.setattr(dl, "_run_depot_downloader", lambda cmd, timeout: "no useful output")
        with pytest.raises(RuntimeError, match="manifest GID"):
            dl.get_manifest_gid(str(tmp_path))

    def test_uses_manifest_only_flag(self, tmp_path, monkeypatch):
        dl = self._make_downloader()
        captured = {}

        def fake_run(cmd, timeout):
            captured["cmd"] = cmd
            return "Manifest 1234567890123456789 @ 2024-01-01\n"

        monkeypatch.setattr(dl, "_run_depot_downloader", fake_run)
        dl.get_manifest_gid(str(tmp_path))
        assert "-manifest-only" in captured["cmd"]


class TestGetBuildId:
    def _make_downloader(self):
        return SteamDownloader(app_id=1422450, depot_id=1422456, branch="public")

    def test_parses_patch_version(self, tmp_path):
        inf = tmp_path / "game" / "citadel" / "steam.inf"
        inf.parent.mkdir(parents=True)
        inf.write_text("PatchVersion=1234\nProductName=deadlock\n")

        dl = self._make_downloader()
        assert dl.get_build_id(str(tmp_path), "game/citadel/steam.inf") == "1234"

    def test_parses_client_version(self, tmp_path):
        inf = tmp_path / "game" / "citadel" / "steam.inf"
        inf.parent.mkdir(parents=True)
        inf.write_text("ClientVersion=5678\n")

        dl = self._make_downloader()
        assert dl.get_build_id(str(tmp_path), "game/citadel/steam.inf") == "5678"

    def test_falls_back_to_hash(self, tmp_path):
        inf = tmp_path / "game" / "citadel" / "steam.inf"
        inf.parent.mkdir(parents=True)
        inf.write_text("SomeOtherKey=foo\n")

        dl = self._make_downloader()
        build_id = dl.get_build_id(str(tmp_path), "game/citadel/steam.inf")
        assert len(build_id) == 16  # sha256 hex prefix

    def test_raises_on_missing_file(self, tmp_path):
        dl = self._make_downloader()
        try:
            dl.get_build_id(str(tmp_path), "game/citadel/steam.inf")
            assert False, "Should have raised"
        except FileNotFoundError:
            pass

    def test_raises_on_empty_file(self, tmp_path):
        inf = tmp_path / "game" / "citadel" / "steam.inf"
        inf.parent.mkdir(parents=True)
        inf.write_text("")

        dl = self._make_downloader()
        try:
            dl.get_build_id(str(tmp_path), "game/citadel/steam.inf")
            assert False, "Should have raised"
        except OSError:
            pass


class TestCollectLooseFiles:
    def _make_downloader(self):
        return SteamDownloader(app_id=1422450, depot_id=1422456, branch="public")

    def test_collects_matching_files(self, tmp_path):
        depot = tmp_path / "depot"
        prefix = depot / "game" / "citadel"
        loc_dir = prefix / "resource" / "localization" / "citadel_gc_hero_names"
        loc_dir.mkdir(parents=True)
        (loc_dir / "english.txt").write_text("hello")
        (loc_dir / "french.txt").write_text("bonjour")

        extract = tmp_path / "extract"
        dl = self._make_downloader()
        result = dl.collect_loose_files(
            str(depot), "game/citadel",
            ["resource/localization/citadel_gc_hero_names/*.txt"],
            str(extract),
        )
        assert sorted(result) == [
            "resource/localization/citadel_gc_hero_names/english.txt",
            "resource/localization/citadel_gc_hero_names/french.txt",
        ]
        assert (extract / "resource/localization/citadel_gc_hero_names/english.txt").exists()

    def test_ignores_non_matching_files(self, tmp_path):
        depot = tmp_path / "depot"
        prefix = depot / "game" / "citadel"
        loc_dir = prefix / "resource" / "localization" / "citadel_gc_hero_names"
        loc_dir.mkdir(parents=True)
        (loc_dir / "english.txt").write_text("hello")
        (loc_dir / "english.bin").write_text("binary")

        extract = tmp_path / "extract"
        dl = self._make_downloader()
        result = dl.collect_loose_files(
            str(depot), "game/citadel",
            ["resource/localization/citadel_gc_hero_names/*.txt"],
            str(extract),
        )
        assert result == ["resource/localization/citadel_gc_hero_names/english.txt"]
        assert not (extract / "resource/localization/citadel_gc_hero_names/english.bin").exists()

    def test_empty_patterns(self, tmp_path):
        dl = self._make_downloader()
        result = dl.collect_loose_files(str(tmp_path), "game/citadel", [], str(tmp_path / "out"))
        assert result == []

    def test_missing_prefix_dir(self, tmp_path):
        dl = self._make_downloader()
        result = dl.collect_loose_files(
            str(tmp_path), "game/citadel",
            ["resource/*.txt"],
            str(tmp_path / "out"),
        )
        assert result == []
