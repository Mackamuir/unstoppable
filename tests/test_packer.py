import tempfile
from pathlib import Path

from src.packer import VPKPacker


def test_stage_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        extract_dir = Path(tmpdir) / "extracted"
        staging_dir = Path(tmpdir) / "staging"
        output_dir = Path(tmpdir) / "output"

        # Create fake extracted files
        (extract_dir / "scripts").mkdir(parents=True)
        (extract_dir / "scripts" / "abilities.vdata_c").write_bytes(b"data1")
        (extract_dir / "scripts" / "npc_units.vdata_c").write_bytes(b"data2")

        packer = VPKPacker(str(staging_dir), str(output_dir), "test.vpk")
        packer.stage_files(
            str(extract_dir),
            {"scripts/abilities.vdata_c", "scripts/npc_units.vdata_c"},
        )

        assert (staging_dir / "scripts" / "abilities.vdata_c").exists()
        assert (staging_dir / "scripts" / "npc_units.vdata_c").exists()


def test_stage_skips_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        extract_dir = Path(tmpdir) / "extracted"
        staging_dir = Path(tmpdir) / "staging"
        output_dir = Path(tmpdir) / "output"

        extract_dir.mkdir(parents=True)
        (extract_dir / "scripts").mkdir()
        (extract_dir / "scripts" / "exists.vdata_c").write_bytes(b"data")

        packer = VPKPacker(str(staging_dir), str(output_dir), "test.vpk")
        packer.stage_files(
            str(extract_dir),
            {"scripts/exists.vdata_c", "scripts/missing.vdata_c"},
        )

        assert (staging_dir / "scripts" / "exists.vdata_c").exists()
        assert not (staging_dir / "scripts" / "missing.vdata_c").exists()


def test_full_build_creates_vpk():
    with tempfile.TemporaryDirectory() as tmpdir:
        extract_dir = Path(tmpdir) / "extracted"
        staging_dir = Path(tmpdir) / "staging"
        output_dir = Path(tmpdir) / "output"

        (extract_dir / "scripts").mkdir(parents=True)
        (extract_dir / "scripts" / "test.vdata_c").write_bytes(b"binary content")

        packer = VPKPacker(str(staging_dir), str(output_dir), "mod_pak99_dir.vpk")
        result = packer.build(str(extract_dir), {"scripts/test.vdata_c"})

        assert result.exists()
        assert result.name == "mod_pak99_dir.vpk"
        assert result.stat().st_size > 0
