import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class State:
    def __init__(self, state_file: str):
        self.state_file = Path(state_file)
        self._data: dict = {}
        self._load()

    def _load(self):
        if self.state_file.exists():
            with open(self.state_file, "r") as f:
                self._data = json.load(f)
            logger.info("Loaded state: build=%s", self._data.get("build_id"))
        else:
            self._data = {}
            logger.info("No existing state file, starting fresh")

    def save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def build_id(self) -> Optional[str]:
        return self._data.get("build_id")

    @build_id.setter
    def build_id(self, value: str):
        self._data["build_id"] = value
        self._data["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.save()

    @property
    def manifest_gid(self) -> Optional[str]:
        return self._data.get("manifest_gid")

    @manifest_gid.setter
    def manifest_gid(self, value: str):
        self._data["manifest_gid"] = value
        self.save()

    @property
    def file_hashes(self) -> Optional[dict]:
        return self._data.get("file_hashes")

    def set_build(self, build_id: str, file_hashes: dict, manifest_gid: Optional[str] = None):
        """Atomically update build_id, file_hashes, and optionally manifest_gid together."""
        self._data["build_id"] = build_id
        self._data["file_hashes"] = file_hashes
        if manifest_gid is not None:
            self._data["manifest_gid"] = manifest_gid
        self._data["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.save()
