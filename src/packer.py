import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


import vpk as vpklib

logger = logging.getLogger(__name__)


class VPKPacker:
    def __init__(self, staging_dir: str, output_dir: str, vpk_name: str):
        self.staging_dir = Path(staging_dir)
        self.output_dir = Path(output_dir)
        self.vpk_name = vpk_name

    def stage_files(self, extract_dir: str, vpk_paths: set[str]):
        """Copy extracted files into the staging directory for packing."""
        if self.staging_dir.exists():
            shutil.rmtree(self.staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        extract_path = Path(extract_dir)
        staged = 0

        for vpk_path in vpk_paths:
            src = extract_path / vpk_path
            dst = self.staging_dir / vpk_path
            if not src.exists():
                logger.warning("Source file missing, skipping: %s", src)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            staged += 1

        logger.info("Staged %d files for packing", staged)

    def write_readme(
        self,
        build_id: str,
        steam_inf_content: Optional[str],
        vpk_file_count: int,
        loose_file_count: int,
    ):
        """Write README.txt into the staging directory."""
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S UTC")

        lines = [
            "unstoppable - Deadlock Vanilla Preservation Mod",
            "=" * 50,
            "",
            "To install this mod:",
            "Extract pak01_dir.vpk to Deadlock/game/citadel/addons/pak01_dir.vpk",
            "",
            "=" * 50,
            "",
            "Maintainer: mack.wtf on discord.",
            f"Built:      {timestamp}",
            f"Build ID:   {build_id}",
            f"VPK files:  {vpk_file_count}",
            f"Loose files:{loose_file_count}",
            f"Total files:{vpk_file_count + loose_file_count}",
            "",
        ]

        if steam_inf_content and steam_inf_content.strip():
            lines += [
                "--- steam.inf ---",
                steam_inf_content.strip(),
                "",
            ]

        lines += [
            "--- About ---",
            "This VPK was automatically generated.",
            "It preserves vanilla Deadlock game files at high priority so they",
            "cannot be overwritten by other mods.",
        ]

        readme_path = self.staging_dir / "README.txt"
        readme_path.write_text("\n".join(lines), encoding="utf-8")
        logger.debug("README.txt written to staging dir")

    def create_vpk(self) -> Path:
        """Create the mod VPK from staged files."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.output_dir / self.vpk_name

        new_pak = vpklib.new(str(self.staging_dir))
        new_pak.save(str(output_path))

        size_mb = output_path.stat().st_size / 1048576
        logger.info("VPK created: %s (%.2f MB)", output_path, size_mb)
        return output_path

    def create_zip(self, vpk_path: Path, zip_name: Optional[str] = None) -> Path:
        """Zip the output VPK for GameBanana upload."""
        zip_path = vpk_path.parent / (zip_name if zip_name else vpk_path.stem + ".zip")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(vpk_path, vpk_path.name)
            readme_path = self.staging_dir / "README.txt"
            if readme_path.exists():
                zf.write(readme_path, "README.txt")
        size_mb = zip_path.stat().st_size / 1048576
        logger.info("Zip created: %s (%.2f MB)", zip_path, size_mb)
        return zip_path

    def build(
        self,
        extract_dir: str,
        vpk_paths: set[str],
        build_id: str = "",
        steam_inf_content: Optional[str] = None,
        vpk_file_count: int = 0,
        loose_file_count: int = 0,
    ) -> Path:
        """Full pipeline: stage files, write README, then create VPK."""
        self.stage_files(extract_dir, vpk_paths)
        self.write_readme(build_id, steam_inf_content, vpk_file_count, loose_file_count)
        return self.create_vpk()
