import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GH_API_BASE = "https://api.github.com"
GH_UPLOAD_BASE = "https://uploads.github.com"


class GitHubPublisher:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def publish(self, zip_path: Path, version: str):
        """Create a GitHub release tagged with the build version and upload the zip."""
        tag = f"v{version}"

        # Check if release already exists
        resp = self.session.get(
            f"{GH_API_BASE}/repos/{self.repo}/releases/tags/{tag}",
        )
        if resp.status_code == 200:
            logger.info("GitHub release %s already exists, skipping", tag)
            return

        # Create release
        resp = self.session.post(
            f"{GH_API_BASE}/repos/{self.repo}/releases",
            json={
                "tag_name": tag,
                "name": f"Build {version}",
                "body": f"Automated release for Deadlock build {version}.",
                "draft": False,
                "prerelease": False,
            },
        )
        resp.raise_for_status()
        release = resp.json()
        release_id = release["id"]
        logger.info("Created GitHub release: %s (id=%d)", tag, release_id)

        # Upload zip asset
        filename = zip_path.name
        upload_resp = self.session.post(
            f"{GH_UPLOAD_BASE}/repos/{self.repo}/releases/{release_id}/assets",
            params={"name": filename},
            headers={
                "Content-Type": "application/zip",
            },
            data=zip_path.read_bytes(),
        )
        upload_resp.raise_for_status()
        logger.info(
            "Uploaded asset %s to GitHub release %s",
            filename, tag,
        )
