import argparse
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
from .github_publisher import GitHubPublisher

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
    # TODO: re-enable after publish testing is complete
    logger.warning("Failure warning posting is disabled (build_id=%s)", build_id)
    return


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
    github_publisher: Optional["GitHubPublisher"] = None,
    force: bool = False,
) -> bool:
    """Execute one update cycle. Returns True if a new VPK was built.

    When force=True, skips manifest_gid and build_id checks.
    """
    extract_dir = tempfile.mkdtemp(prefix="unstoppable_")
    detected_build_id = None
    try:
        depot_dir = config.output.depot_cache_dir

        # Fast check: download only the depot manifest (no game files)
        manifest_gid = downloader.get_manifest_gid(depot_dir)
        if not force and manifest_gid == state.manifest_gid:
            logger.debug("No update (manifest_gid=%s)", manifest_gid)
            return False

        logger.info(
            "Manifest changed: %s -> %s",
            state.manifest_gid or "(first run)", manifest_gid,
        )

        downloader.download_depot(depot_dir)

        # Read build ID from the downloaded depot
        build_id = downloader.get_build_id(depot_dir, config.steam_inf_path)

        if not force and build_id == state.build_id:
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

        if github_publisher:
            try:
                github_publisher.publish(zip_path=output_zip, version=build_id)
            except Exception:
                logger.exception("GitHub release failed")

        return True

    except Exception:
        if publisher and detected_build_id:
            _try_post_failure_warning(publisher, detected_build_id, state)
        raise
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def _init_components(config: AppConfig):
    """Create shared components (state, downloader, packer, publisher)."""
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
    if gb.enabled and gb.username and gb.password:
        publisher = GameBananaPublisher(
            username=gb.username,
            password=gb.password,
            mod_id=gb.mod_id,
        )

    gh_publisher = None
    gh = config.github
    if gh.enabled and gh.token and gh.repo:
        gh_publisher = GitHubPublisher(token=gh.token, repo=gh.repo)

    return state, downloader, packer, publisher, gh_publisher


def cmd_run(config: AppConfig):
    """Run the polling loop (default behavior)."""
    logger.info(
        "unstoppable starting (app=%d, poll=%ds, vpk_patterns=%d, loose_patterns=%d)",
        config.steam.app_id,
        config.steam.poll_interval_seconds,
        len(config.tracked_vpk_files),
        len(config.tracked_loose_files),
    )

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    state, downloader, packer, publisher, gh_publisher = _init_components(config)

    if publisher:
        logger.info("GameBanana publishing enabled (mod=%d)", config.gamebanana.mod_id)
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
    elif config.gamebanana.enabled:
        logger.warning("GameBanana enabled but GB_USERNAME/GB_PASSWORD not set: skipping")

    if gh_publisher:
        logger.info("GitHub publishing enabled (repo=%s)", config.github.repo)
    elif config.github.enabled:
        logger.warning("GitHub enabled but GITHUB_TOKEN not set: skipping")

    while not _shutdown:
        try:
            run_update_cycle(downloader, packer, state, config, publisher, gh_publisher)
        except Exception:
            logger.exception("Error in update cycle, will retry next poll")

        for _ in range(config.steam.poll_interval_seconds):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("unstoppable shutdown complete")


def cmd_update(config: AppConfig, args: argparse.Namespace):
    """Force a single update cycle, skipping manifest/build checks."""
    state, downloader, packer, publisher, gh_publisher = _init_components(config)
    logger.info("Forcing update cycle")
    built = run_update_cycle(downloader, packer, state, config, publisher, gh_publisher, force=True)
    if built:
        print("Update complete: new VPK built and published")
    else:
        print("Update cycle ran but no files were found to pack")


def _resolve_zip(config: AppConfig, args: argparse.Namespace) -> tuple[Path, str]:
    """Find the zip to publish and extract its version string."""
    state = State(config.state.file)

    if args.zip:
        zip_path = Path(args.zip)
    else:
        output_dir = Path(config.output.output_dir)
        zips = sorted(output_dir.glob("unstoppable_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not zips:
            print(f"Error: No unstoppable_*.zip files found in {output_dir}")
            sys.exit(1)
        zip_path = zips[0]

    if not zip_path.exists():
        print(f"Error: Zip file not found: {zip_path}")
        sys.exit(1)

    version = zip_path.stem.removeprefix("unstoppable_") or state.build_id or "unknown"
    return zip_path, version


def cmd_publish_gb(config: AppConfig, args: argparse.Namespace):
    """Force-publish a zip to GameBanana."""
    _, _, _, publisher, _ = _init_components(config)

    if not publisher:
        print("Error: GameBanana publishing is not configured (check config.yaml and GB_USERNAME/GB_PASSWORD)")
        sys.exit(1)

    zip_path, version = _resolve_zip(config, args)
    print(f"Publishing {zip_path} (version={version}) to GameBanana mod {config.gamebanana.mod_id}")
    publisher.publish(zip_path=zip_path, version=version, config=config)
    print("Publish complete")


def cmd_publish_gh(config: AppConfig, args: argparse.Namespace):
    """Force-publish a zip as a GitHub release."""
    _, _, _, _, gh_publisher = _init_components(config)

    if not gh_publisher:
        print("Error: GitHub publishing is not configured (check config.yaml and GITHUB_TOKEN)")
        sys.exit(1)

    zip_path, version = _resolve_zip(config, args)
    print(f"Publishing {zip_path} (version={version}) to GitHub {config.github.repo}")
    gh_publisher.publish(zip_path=zip_path, version=version)
    print("Publish complete")


def cmd_status(config: AppConfig, args: argparse.Namespace):
    """Show current state."""
    state = State(config.state.file)
    data = state._data

    print(f"State file: {config.state.file}")
    print(f"Build ID:   {data.get('build_id', '(none)')}")
    print(f"Manifest:   {data.get('manifest_gid', '(none)')}")
    print(f"Updated:    {data.get('last_updated', '(never)')}")

    pending = data.get("pending_failure_update_id")
    if pending:
        print(f"Pending failure warning: update_id={pending}")

    hashes = data.get("file_hashes", {})
    print(f"Tracked files: {len(hashes)}")

    # Check output directory for zips
    output_dir = Path(config.output.output_dir)
    zips = sorted(output_dir.glob("unstoppable_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if zips:
        latest = zips[0]
        size_mb = latest.stat().st_size / 1048576
        print(f"Latest zip: {latest.name} ({size_mb:.2f} MB)")
    else:
        print("Latest zip: (none)")


def cmd_reset(config: AppConfig, args: argparse.Namespace):
    """Reset the state file."""
    if not args.confirm:
        print("This will clear the build_id, manifest_gid, and file hashes.")
        print("The next poll cycle will re-download and rebuild everything.")
        print("Run with --confirm to proceed.")
        sys.exit(1)

    state_path = Path(config.state.file)
    if state_path.exists():
        state_path.unlink()
        print(f"State file deleted: {state_path}")
    else:
        print(f"No state file to delete: {state_path}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unstoppable",
        description="Auto-updating Deadlock vanilla preservation mod",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="Start the polling loop (default)")
    sub.add_parser("update", help="Force a single update cycle")

    pub_gb = sub.add_parser("publish_gb", help="Force-publish a zip to GameBanana")
    pub_gb.add_argument("--zip", help="Path to zip file (defaults to latest in output dir)")

    pub_gh = sub.add_parser("publish_gh", help="Force-publish a zip as a GitHub release")
    pub_gh.add_argument("--zip", help="Path to zip file (defaults to latest in output dir)")

    sub.add_parser("status", help="Show current state and build info")

    reset = sub.add_parser("reset", help="Reset state file to force a fresh run")
    reset.add_argument("--confirm", action="store_true", help="Confirm the reset")

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)

    handlers = {
        "run": lambda: cmd_run(config),
        "update": lambda: cmd_update(config, args),
        "publish_gb": lambda: cmd_publish_gb(config, args),
        "publish_gh": lambda: cmd_publish_gh(config, args),
        "status": lambda: cmd_status(config, args),
        "reset": lambda: cmd_reset(config, args),
        None: lambda: cmd_run(config),
    }

    handler = handlers.get(args.command)
    if handler:
        handler()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
