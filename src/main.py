import sys
import time
import signal
import logging
import json
import tempfile
import shutil
from pathlib import Path
from typing import Optional

from .config import load_config, AppConfig
from .state import State
from .downloader import SteamDownloader
from .packer import VPKPacker
from .publisher import GameBananaPublisher

logger = logging.getLogger("unstoppable")

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received (signal=%d)", signum)
    _shutdown = True


class JSONFormatter(logging.Formatter):
    _base_keys = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)

    def format(self, record):
        entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in self._base_keys and k not in ("message", "msg")
        }
        if extras:
            entry["extra"] = extras
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging(config: AppConfig):
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.logging.level.upper()))

    handler = logging.StreamHandler(sys.stdout)
    if config.logging.format == "structured":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    root.addHandler(handler)


def _try_post_failure_warning(publisher: "GameBananaPublisher", build_id: str, state: "State"):
    try:
        update_id = publisher.post_failure_warning(build_id)
        if update_id:
            state.pending_failure_update_id = update_id
    except Exception:
        logger.exception("Failed to post failure warning to GameBanana")


def _compute_file_hashes(extract_dir: str, paths: set[str]) -> dict[str, str]:
    """Return a mapping of path -> SHA256 hex for each file in paths."""
    import hashlib
    hashes = {}
    for p in paths:
        file = Path(extract_dir) / p
        if file.exists():
            hashes[p] = hashlib.sha256(file.read_bytes()).hexdigest()
    return hashes

def run_update_cycle(
    downloader: SteamDownloader,
    packer: VPKPacker,
    state: State,
    config: AppConfig,
    publisher: Optional["GameBananaPublisher"] = None,
) -> bool:
    """Execute one update cycle. Returns True if a new VPK was built."""
    extract_dir = tempfile.mkdtemp(prefix="unstoppable_")
    detected_build_id = None
    try:
        depot_dir = config.output.depot_cache_dir

        # Fast check: download only the depot manifest (no game files)
        manifest_gid = downloader.get_manifest_gid(depot_dir)
        if manifest_gid == state.manifest_gid:
            logger.debug("No update (manifest_gid=%s)", manifest_gid)
            return False

        logger.info(
            "Manifest changed: %s -> %s",
            state.manifest_gid or "(first run)", manifest_gid,
        )

        downloader.download_depot(depot_dir)

        # Read build ID from the downloaded depot
        build_id = downloader.get_build_id(depot_dir, config.steam_inf_path)

        if build_id == state.build_id:
            # Steam repackaged the depot but the game version didn't change
            logger.info("Depot repackaged but build unchanged (build=%s), updating manifest_gid", build_id)
            state.manifest_gid = manifest_gid
            return False

        detected_build_id = build_id

        steam_inf_file = Path(depot_dir) / config.steam_inf_path
        steam_inf_content = steam_inf_file.read_text() if steam_inf_file.exists() else None

        logger.info(
            "Update detected: %s -> %s",
            state.build_id or "(first run)", build_id,
        )

        # Extract VPK files from the depot's VPK
        vpk_files = []
        if config.tracked_vpk_files:
            vpk_path = Path(depot_dir) / config.source_vpk_path
            if not vpk_path.exists():
                raise FileNotFoundError(f"Source VPK not found: {vpk_path}")
            vpk_files = downloader.extract_vpk_files(
                vpk_path, config.tracked_vpk_files, extract_dir,
            )

        # Collect loose files from the depot
        loose_files = []
        if config.tracked_loose_files:
            loose_files = downloader.collect_loose_files(
                depot_dir, config.loose_content_prefix,
                config.tracked_loose_files, extract_dir,
            )

        all_paths = set(vpk_files) | set(loose_files)
        if not all_paths:
            logger.warning("No tracked files found in depot")
            state.build_id = build_id
            return False

        current_hashes = _compute_file_hashes(extract_dir, all_paths)

        logger.info(
            "Build %s",
            build_id
        )

        # Pack into mod VPK
        output_vpk = packer.build(
            extract_dir,
            all_paths,
            build_id=build_id,
            steam_inf_content=steam_inf_content,
            vpk_file_count=len(vpk_files),
            loose_file_count=len(loose_files),
        )
        output_zip = packer.create_zip(output_vpk, zip_name=f"unstoppable_{build_id}.zip")
        output_vpk.unlink()
        logger.debug("Deleted VPK after zipping: %s", output_vpk)

        logger.info(
            "Update complete: output=%s, zip=%s, vpk_files=%d, loose_files=%d",
            output_vpk, output_zip, len(vpk_files), len(loose_files),
        )

        if publisher:
            try:
                publisher.publish(
                    zip_path=output_zip,
                    version=build_id,
                    config=config,
                )
                state.set_build(build_id, current_hashes, manifest_gid=manifest_gid)
                pending_id = state.pending_failure_update_id
                if pending_id:
                    try:
                        publisher.delete_update(pending_id)
                        state.pending_failure_update_id = None
                    except Exception:
                        logger.exception("Failed to delete pending failure warning (update_id=%d)", pending_id)
            except Exception:
                logger.exception("GameBanana publish failed (VPK still saved locally)")
                _try_post_failure_warning(publisher, build_id, state)
        else:
            state.set_build(build_id, current_hashes, manifest_gid=manifest_gid)

        return True

    except Exception:
        if publisher and detected_build_id:
            _try_post_failure_warning(publisher, detected_build_id, state)
        raise
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def main():
    config = load_config()
    setup_logging(config)

    logger.info(
        "unstoppable starting (app=%d, poll=%ds, vpk_patterns=%d, loose_patterns=%d)",
        config.steam.app_id,
        config.steam.poll_interval_seconds,
        len(config.tracked_vpk_files),
        len(config.tracked_loose_files),
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    state = State(config.state.file)
    downloader = SteamDownloader(
        app_id=config.steam.app_id,
        depot_id=config.steam.depot_id,
        branch=config.steam.branch,
        username=config.steam.username,
        password=config.steam.password,
    )
    packer = VPKPacker(
        staging_dir=config.output.staging_dir,
        output_dir=config.output.output_dir,
        vpk_name=config.output.vpk_name,
    )

    publisher = None
    gb = config.gamebanana
    if gb.enabled:
        if gb.username and gb.password:
            publisher = GameBananaPublisher(
                username=gb.username,
                password=gb.password,
                mod_id=gb.mod_id,
                section=gb.section,
            )
            logger.info("GameBanana publishing enabled (mod=%d)", gb.mod_id)

            # Sync local state with the version actually published on GameBanana
            try:
                published_version = publisher.get_published_version()
                if published_version and published_version != state.build_id:
                    logger.warning(
                        "Local build_id (%s) does not match GameBanana version (%s), resetting to published version",
                        state.build_id or "(none)", published_version,
                    )
                    state.build_id = published_version
            except Exception:
                logger.exception("Failed to check GameBanana published version, continuing with local state")
        else:
            logger.warning("GameBanana enabled but GB_USERNAME/GB_PASSWORD not set: skipping")

    while not _shutdown:
        try:
            run_update_cycle(downloader, packer, state, config, publisher)
        except Exception:
            logger.exception("Error in update cycle, will retry next poll")

        for _ in range(config.steam.poll_interval_seconds):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("unstoppable shutdown complete")


if __name__ == "__main__":
    main()
