from src.path_mapper import matches_any_pattern


class TestMatchesAnyPattern:
    def test_simple_glob(self):
        assert matches_any_pattern("scripts/abilities.vdata_c", ["scripts/*.vdata_c"])

    def test_no_match_wrong_dir(self):
        assert not matches_any_pattern("other/abilities.vdata_c", ["scripts/*.vdata_c"])

    def test_recursive_glob(self):
        assert matches_any_pattern(
            "soundevents/hero/astro/astro.vsndevts_c",
            ["soundevents/**/*.vsndevts_c"],
        )

    def test_recursive_glob_deep(self):
        assert matches_any_pattern(
            "resource/localization/citadel_override/sub/file.txt",
            ["resource/localization/**/*.txt"],
        )

    def test_no_match_outside(self):
        assert not matches_any_pattern("other/file.txt", ["resource/**/*.txt"])

    def test_multiple_patterns(self):
        patterns = ["scripts/*.vdata_c", "resource/**/*.txt"]
        assert matches_any_pattern("scripts/abilities.vdata_c", patterns)
        assert matches_any_pattern("resource/localization/en.txt", patterns)
        assert not matches_any_pattern("models/hero.vmdl_c", patterns)

    def test_exact_filename(self):
        assert matches_any_pattern("scripts/items/items_game.txt", ["scripts/items/*.txt"])

    def test_no_match_partial_dir(self):
        assert not matches_any_pattern("scripts/sub/abilities.vdata_c", ["scripts/*.vdata_c"])
