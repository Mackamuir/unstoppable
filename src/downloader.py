import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import vpk as vpklib

from .retry import retry
from .path_mapper import matches_any_pattern

logger = logging.getLogger(__name__)

DEPOT_DOWNLOADER_CMD = "DepotDownloader"


class SteamDownloader:
    def __init__(
        self,
        app_id: int,
        depot_id: int,
        branch: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.app_id = app_id
        self.depot_id = depot_id
        self.branch = branch
        self.username = username
        self.password = password

    def _build_base_cmd(self, download_dir: str, filelist_path: str) -> list[str]:
        cmd = [
            DEPOT_DOWNLOADER_CMD,
            "-app", str(self.app_id),
            "-depot", str(self.depot_id),
            "-branch", self.branch,
            "-dir", download_dir,
            "-filelist", filelist_path,
            "-max-downloads", "8",
        ]
        if self.username:
            cmd.extend(["-username", self.username])
        if self.password:
            cmd.extend(["-password", self.password])
        cmd.append("-remember-password")
        return cmd

    def _run_depot_downloader(self, cmd: list[str], timeout: int = 600) -> str:
        logger.info("Running DepotDownloader: %s", " ".join(cmd[:6]))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.error(
                "DepotDownloader failed (rc=%d): %s",
                result.returncode, result.stderr[-500:] if result.stderr else "",
            )
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        return result.stdout

    @retry(max_attempts=2, base_delay=5.0, exceptions=(subprocess.CalledProcessError, OSError))
    def check_build_id(self, download_dir: str, steam_inf_path: str) -> str:
        """Download steam.inf and parse the build ID for version checking."""
        dl_path = Path(download_dir)
        dl_path.mkdir(parents=True, exist_ok=True)

        filelist = dl_path / "_filelist_inf.txt"
        filelist.write_text(steam_inf_path + "\n")

        cmd = self._build_base_cmd(str(dl_path), str(filelist))
        self._run_depot_downloader(cmd, timeout=180)

        inf_file = dl_path / steam_inf_path
        if not inf_file.exists():
            raise FileNotFoundError(f"steam.inf not found: {inf_file}")

        content = inf_file.read_text()
        if not content.strip():
            raise OSError("steam.inf downloaded but is empty - chunk download likely failed")
        # Parse PatchVersion or ClientVersion from steam.inf
        for line in content.splitlines():
            if line.startswith("PatchVersion=") or line.startswith("ClientVersion="):
                build_id = line.split("=", 1)[1].strip()
                logger.info("Current build: %s", build_id)
                return build_id

        # Fallback: use the full content hash as build ID
        import hashlib
        build_id = hashlib.sha256(content.encode()).hexdigest()[:16]
        logger.warning("Could not parse version from steam.inf, using hash: %s", build_id)
        return build_id

    @retry(max_attempts=2, base_delay=10.0, exceptions=(subprocess.CalledProcessError, OSError))
    def download_vpk(self, download_dir: str, source_vpk_path: str) -> Path:
        """Download pak01_dir.vpk and all its data archives from the depot."""
        dl_path = Path(download_dir)
        dl_path.mkdir(parents=True, exist_ok=True)

        filelist = dl_path / "_filelist_vpk.txt"
        # Match all pak01 VPK parts (dir + data archives)
        vpk_base = source_vpk_path.replace("_dir.vpk", "")
        filelist.write_text(f"regex:{re.escape(vpk_base)}_.*\\.vpk\n")

        cmd = self._build_base_cmd(str(dl_path), str(filelist))
        self._run_depot_downloader(cmd, timeout=600)

        vpk_path = dl_path / source_vpk_path
        if not vpk_path.exists():
            raise FileNotFoundError(f"VPK not found after download: {vpk_path}")

        size_mb = vpk_path.stat().st_size / 1048576
        logger.info("VPK downloaded: %s (%.1f MB)", vpk_path, size_mb)
        return vpk_path

    def extract_vpk_files(
        self,
        vpk_path: Path,
        patterns: list[str],
        extract_dir: str,
    ) -> list[str]:
        """Extract files matching patterns from the VPK.

        Returns the list of extracted VPK-internal paths.
        """
        extract_path = Path(extract_dir)
        extract_path.mkdir(parents=True, exist_ok=True)

        try:
            pak = vpklib.open(str(vpk_path))
        except ValueError as e:
            logger.warning("Corrupted VPK (invalid magic), deleting for redownload: %s", vpk_path)
            vpk_base = vpk_path.name.replace("_dir.vpk", "")
            for sibling in vpk_path.parent.glob(f"{vpk_base}*.vpk"):
                sibling.unlink(missing_ok=True)
            raise OSError("Corrupted VPK deleted, redownload required") from e
        extracted = []

        for file_path in pak:
            if matches_any_pattern(file_path, patterns):
                out_file = extract_path / file_path
                out_file.parent.mkdir(parents=True, exist_ok=True)
                with open(out_file, "wb") as f:
                    f.write(pak[file_path].read())
                extracted.append(file_path)

        logger.info("Extracted %d files from VPK", len(extracted))
        return extracted

    @retry(max_attempts=2, base_delay=10.0, exceptions=(subprocess.CalledProcessError, OSError))
    def download_loose_files(
        self,
        patterns: list[str],
        loose_prefix: str,
        download_dir: str,
        extract_dir: str,
    ) -> list[str]:
        """Download loose files from the depot and stage them.

        Since we can't glob the depot, we download everything under the
        loose_prefix and then filter locally with our patterns.

        Returns list of extracted relative paths.
        """
        if not patterns:
            return []

        dl_path = Path(download_dir)
        dl_path.mkdir(parents=True, exist_ok=True)

        # Download the entire loose_prefix directory
        filelist = dl_path / "_filelist_loose.txt"
        filelist.write_text(f"regex:{re.escape(loose_prefix)}/.*\n")

        cmd = self._build_base_cmd(str(dl_path), str(filelist))
        self._run_depot_downloader(cmd, timeout=300)

        # Walk downloaded files and filter with our patterns
        extract_path = Path(extract_dir)
        prefix_path = dl_path / loose_prefix
        extracted = []

        if not prefix_path.exists():
            logger.warning("Loose prefix directory not found: %s", prefix_path)
            return []

        for file in prefix_path.rglob("*"):
            if not file.is_file():
                continue
            # Get path relative to the prefix (e.g., resource/localization/file.txt)
            rel = file.relative_to(prefix_path).as_posix()
            if matches_any_pattern(rel, patterns):
                out_file = extract_path / rel
                out_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, out_file)
                extracted.append(rel)

        logger.info("Downloaded %d loose files", len(extracted))
        return extracted
