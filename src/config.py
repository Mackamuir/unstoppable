import os
import yaml
from dataclasses import dataclass
from typing import Optional


@dataclass
class GameBananaConfig:
    enabled: bool
    mod_id: int
    username: Optional[str] = None
    password: Optional[str] = None


@dataclass
class SteamConfig:
    app_id: int
    depot_id: int
    branch: str
    poll_interval_seconds: int
    username: Optional[str] = None
    password: Optional[str] = None


@dataclass
class OutputConfig:
    vpk_name: str
    staging_dir: str
    output_dir: str
    depot_cache_dir: str


@dataclass
class StateConfig:
    file: str


@dataclass
class LoggingConfig:
    level: str
    format: str


@dataclass
class GitHubConfig:
    enabled: bool
    repo: str
    token: Optional[str] = None


@dataclass
class AppConfig:
    steam: SteamConfig
    output: OutputConfig
    state: StateConfig
    logging: LoggingConfig
    gamebanana: GameBananaConfig
    github: GitHubConfig
    source_vpk_path: str
    steam_inf_path: str
    tracked_vpk_files: list[str]
    loose_content_prefix: str
    tracked_loose_files: list[str]


def load_config(config_path: str = "config.yaml") -> AppConfig:
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    gb = raw.get("gamebanana", {})
    gh = raw.get("github", {})
    return AppConfig(
        gamebanana=GameBananaConfig(
            enabled=gb.get("enabled", False),
            mod_id=gb.get("mod_id", 0),
            username=os.environ.get("GB_USERNAME"),
            password=os.environ.get("GB_PASSWORD"),
        ),
        github=GitHubConfig(
            enabled=gh.get("enabled", False),
            repo=gh.get("repo", ""),
            token=os.environ.get("GITHUB_TOKEN"),
        ),
        steam=SteamConfig(
            app_id=raw["steam"]["app_id"],
            depot_id=raw["steam"]["depot_id"],
            branch=raw["steam"]["branch"],
            poll_interval_seconds=raw["steam"]["poll_interval_seconds"],
            username=os.environ.get("STEAM_USERNAME"),
            password=os.environ.get("STEAM_PASSWORD"),
        ),
        output=OutputConfig(
            vpk_name=raw["output"]["vpk_name"],
            staging_dir=raw["output"]["staging_dir"],
            output_dir=raw["output"]["output_dir"],
            depot_cache_dir=raw["output"]["depot_cache_dir"],
        ),
        state=StateConfig(file=raw["state"]["file"]),
        logging=LoggingConfig(
            level=raw["logging"]["level"],
            format=raw["logging"]["format"],
        ),
        source_vpk_path=raw["source_vpk_path"],
        steam_inf_path=raw["steam_inf_path"],
        tracked_vpk_files=raw.get("tracked_vpk_files", []),
        loose_content_prefix=raw["loose_content_prefix"],
        tracked_loose_files=raw.get("tracked_loose_files", []),
    )
