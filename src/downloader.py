import logging
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

    def _build_base_cmd(self, download_dir: str) -> list[str]:
        cmd = [
            DEPOT_DOWNLOADER_CMD,
            "-app", str(self.app_id),
            "-depot", str(self.depot_id),
            "-branch", self.branch,
            "-dir", download_dir,
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

    @retry(max_attempts=2, base_delay=10.0, exceptions=(subprocess.CalledProcessError, OSError))
    def download_depot(self, depot_dir: str):
        """Download or update the entire depot. DepotDownloader handles incremental updates."""
        Path(depot_dir).mkdir(parents=True, exist_ok=True)
        cmd = self._build_base_cmd(depot_dir)
        cmd.append("-validate")
        self._run_depot_downloader(cmd, timeout=1800)
        logger.info("Depot download complete: %s", depot_dir)

    def get_build_id(self, depot_dir: str, steam_inf_path: str) -> str:
        """Read build ID from the already-downloaded steam.inf."""
        inf_file = Path(depot_dir) / steam_inf_path
        if not inf_file.exists():
            raise FileNotFoundError(f"steam.inf not found: {inf_file}")

        content = inf_file.read_text()
        if not content.strip():
            raise OSError("steam.inf exists but is empty")

        for line in content.splitlines():
            if line.startswith("PatchVersion=") or line.startswith("ClientVersion="):
                build_id = line.split("=", 1)[1].strip()
                logger.info("Current build: %s", build_id)
                return build_id

        import hashlib
        build_id = hashlib.sha256(content.encode()).hexdigest()[:16]
        logger.warning("Could not parse version from steam.inf, using hash: %s", build_id)
        return build_id

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

        pak = vpklib.open(str(vpk_path))
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

    def collect_loose_files(
        self,
        depot_dir: str,
        loose_prefix: str,
        patterns: list[str],
        extract_dir: str,
    ) -> list[str]:
        """Copy matching loose files from the depot to the extract directory.

        Returns list of extracted relative paths (relative to loose_prefix).
        """
        if not patterns:
            return []

        prefix_path = Path(depot_dir) / loose_prefix
        extract_path = Path(extract_dir)
        extracted = []

        if not prefix_path.exists():
            logger.warning("Loose prefix directory not found: %s", prefix_path)
            return []

        for file in prefix_path.rglob("*"):
            if not file.is_file():
                continue
            rel = file.relative_to(prefix_path).as_posix()
            if matches_any_pattern(rel, patterns):
                out_file = extract_path / rel
                out_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, out_file)
                extracted.append(rel)

        logger.info("Collected %d loose files from depot", len(extracted))
        return extracted
