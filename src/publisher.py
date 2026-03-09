import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .config import AppConfig

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MB - matches observed upload chunks
GB_BASE = "https://gamebanana.com"
GB_API_BASE = "https://gamebanana.com/apiv11"
GB_UPLOAD_URL = f"{GB_BASE}/responders/jfuare"


@dataclass
class UploadResult:
    file_row_id: int
    upload_receipt_id: str
    filename: str


class GameBananaPublisher:
    def __init__(self, username: str, password: str, mod_id: int, section: str = "Mod"):
        self.username = username
        self.password = password
        self.mod_id = mod_id
        self.section = section
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
        })

    def _request(self, method, url, max_retries=5, base_delay=10.0, max_delay=120.0, **kwargs):
        """HTTP request with exponential backoff retry on transient 5xx/connection errors."""
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == max_retries:
                    raise
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                logger.warning(
                    "%s %s attempt %d/%d connection error: %s (retrying in %.0fs)",
                    method.upper(), url, attempt, max_retries, e, delay,
                )
                time.sleep(delay)
                continue

            if resp.status_code < 500:
                return resp

            if attempt == max_retries:
                resp.raise_for_status()

            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            logger.warning(
                "%s %s attempt %d/%d returned %d (retrying in %.0fs)",
                method.upper(), url, attempt, max_retries, resp.status_code, delay,
            )
            time.sleep(delay)

    def authenticate(self):
        """Authenticate with GameBanana API."""
        self.session.headers.pop("Authorization", None)
        self.session.cookies.clear()
        resp = self._request(
            "POST",
            f"{GB_API_BASE}/Member/Authenticate",
            json={"_sUsername": self.username, "_sPassword": self.password},
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info("Auth response: %s", data)
        token = data.get("_sToken") or data.get("token")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        logger.info("Auth cookies: %s", dict(self.session.cookies))
        logger.info("Authenticated with GameBanana as %s", self.username)

    def _get_sdpid(self) -> str:
        """Scrape the edit page to get the session-specific upload token (sdpid)."""
        edit_url = f"{GB_BASE}/mods/edit/{self.mod_id}"
        resp = self._request("GET", edit_url, headers={"accept": "text/html"})
        resp.raise_for_status()

        html = resp.text
        match = re.search(r'["\']sdpid["\']\s*[,:]?\s*["\']([a-f0-9]{32})["\']', html)
        if match:
            sdpid = match.group(1)
            logger.info("Found sdpid: %s", sdpid)
            return sdpid

        logger.warning("Could not find sdpid in edit page. Snippet: %r", html[:2000])
        raise RuntimeError("Could not find sdpid in edit page HTML")

    def upload_zip(self, zip_path: Path) -> UploadResult:
        """Upload zip file in 1 MB chunks. Returns UploadResult with file row ID and receipt."""
        sdpid = self._get_sdpid()

        total_size = zip_path.stat().st_size
        filename = zip_path.name
        result = None

        logger.info("Uploading %s (%.2f MB)", filename, total_size / 1048576)

        with open(zip_path, "rb") as f:
            offset = 0
            chunk_num = 0
            while offset < total_size:
                chunk = f.read(CHUNK_SIZE)
                end = offset + len(chunk) - 1
                chunk_num += 1

                resp = self._request(
                    "POST",
                    GB_UPLOAD_URL,
                    headers={
                        "content-range": f"bytes {offset}-{end}/{total_size}",
                        "x-requested-with": "XMLHttpRequest",
                        "origin": GB_BASE,
                        "referer": f"{GB_BASE}/mods/edit/{self.mod_id}",
                        "accept": "application/json, text/javascript, */*; q=0.01",
                    },
                    files=[
                        ("sdpid", (None, sdpid)),
                        ("files[]", (filename, chunk, "application/x-zip-compressed")),
                    ],
                )
                logger.info(
                    "Chunk %d response: status=%d, content_type=%r, body=%r",
                    chunk_num, resp.status_code,
                    resp.headers.get("Content-Type"), resp.text[:500],
                )
                resp.raise_for_status()

                if not resp.text:
                    offset += len(chunk)
                    continue

                data = resp.json()

                if "files" in data:
                    file_info = data["files"][0]
                    if "error" in file_info:
                        raise RuntimeError(f"Upload error from server: {file_info['error']}")
                    result = UploadResult(
                        file_row_id=file_info["_idFileRow"],
                        upload_receipt_id=file_info["_sUploadReceiptId"],
                        filename=file_info["_sFile"],
                    )
                    logger.info(
                        "Upload complete: file=%s, id=%d, receipt=%s",
                        result.filename, result.file_row_id, result.upload_receipt_id,
                    )
                else:
                    logger.info(
                        "Chunk %d: %d/%d bytes uploaded",
                        chunk_num,
                        data.get("_nCurrentFilesize", end + 1),
                        total_size,
                    )

                offset += len(chunk)

        if result is None:
            raise RuntimeError("Upload finished but no _idFileRow in response")
        return result

    def _scrape_edit_page(self) -> dict:
        """GET the edit page and scrape the per-session dynamic fields."""
        resp = self._request(
            "GET",
            f"{GB_BASE}/mods/edit/{self.mod_id}",
            headers={"accept": "text/html"},
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # CSRF: hidden input with a long base64-ish value whose name starts with _
        csrf_name = csrf_value = None
        for inp in soup.find_all("input", {"type": "hidden"}):
            name = inp.get("name", "")
            value = inp.get("value", "")
            if name.startswith("_") and len(value) > 50 and re.match(r'^[A-Za-z0-9+/=]+$', value):
                csrf_name = name
                csrf_value = value
                break

        if not csrf_name:
            raise RuntimeError("Could not find CSRF token in edit page")
        logger.info("Found CSRF field: name=%r", csrf_name)

        # Image ticket IDs - may be in JS/data attrs, not plain inputs.
        # Try HTML inputs first, then fall back to raw regex on the page source.
        ticket_ids = [inp.get("value", "") for inp in soup.find_all("input", {"name": "_sTicketId"})]
        if not ticket_ids:
            ticket_ids = re.findall(r'[Tt]icket[Ii]d["\']?\s*[:=,]\s*["\']([a-f0-9]{32})["\']', resp.text)
        logger.info("Found ticket IDs: %s", ticket_ids)

        return {"csrf_name": csrf_name, "csrf_value": csrf_value, "ticket_ids": ticket_ids}

    def post_edit(self, upload: UploadResult, version: str, config: AppConfig):
        """Submit the mod edit form with the new file row ID."""
        page = self._scrape_edit_page()

        ticket_ids = page["ticket_ids"]
        # Pad to 2 in case fewer are found
        while len(ticket_ids) < 2:
            ticket_ids.append("")

        image_json = json.dumps([
            {"name": "_sCaption", "value": "icon"},
            {"name": "_sTicketId", "value": ticket_ids[0]},
            {"name": "_sCaption", "value": ""},
            {"name": "_sTicketId", "value": ticket_ids[1]},
        ])

        file_json = json.dumps([[
            {"name": "_sDescription", "value": ""},
            {"name": "_sVersion", "value": ""},
            {"name": "_idFileRow", "value": str(upload.file_row_id)},
            {"name": "_sUploadReceiptId", "value": upload.upload_receipt_id},
        ]])

        file_list_items = [
            f"<li>Deadlock/{config.source_vpk_path}:{pattern}</li>"
            for pattern in config.tracked_vpk_files
        ] + [
            f"<li>{pattern}</li>"
            for pattern in config.tracked_loose_files
        ]
        file_list_html = "<ul>" + "".join(file_list_items) + "</ul>"

        description = (
            "Unstoppable is a mod that holds vanilla files so that other mods are unable to overwrite them.<br>"
            "Tons of mods try to all replace the same files, or replace files and break UI.<br><br>"
            "The following files are included in this mod:<br><br>"
            + file_list_html
            + "<br>"
            "This mod should be placed at Deadlock/game/citadel/addons/pak01_dir.vpk, or enabled first in the mod manager. "
            "If you believe it is breaking other mod (lol) then place unstoppable at pak02 and the first mod at pak01.<br><br>"
            "If you have any files you wish for this mod to track, feel free to leave a comment.<br><br>"
            "In theory this mod will always be current."
        )

        # Build form body. Field order matches the browser capture.
        # Only CSRF name/value, ticket IDs, image JSON, and file row fields are dynamic.
        form = [
            (page["csrf_name"], page["csrf_value"]),
            ("0de3113420b848a4ad714de755dba0d9", "Unstoppable - Stop mods breaking."),
            ("5a67d92aeb8ff13269966e4fffb78c3f", "20948"),
            ("5a67d92aeb8ff13269966e4fffb78c3f_chld", "31710"),
            ("4d9bc49649acb2ee59b81cfe2a5e13b6", description),
            ("9e7ff89ddc9e545ad5707a524ea649f1", ""),
            ("c90417b1da1efc669078ca25a5774986", ""),
            ("4b6936c0f58e7a99065e846f89b11a29", "false"),
            ("dada83bb525018a6876422f493d69291", "true"),
            ("dfc7e6be14e75c67f5219d3cbae51035[1][group_name]", "Developer"),
            ("dfc7e6be14e75c67f5219d3cbae51035[1][author_userids][]", "5279634"),
            ("dfc7e6be14e75c67f5219d3cbae51035[1][author_names][]", "mack.wtf"),
            ("dfc7e6be14e75c67f5219d3cbae51035[1][author_offsite_urls][]", ""),
            ("dfc7e6be14e75c67f5219d3cbae51035[1][author_roles][]", "dude who made it"),
            ("e074a85f9a0a6ae7c463898fcbf66b74", "0"),
            ("9721299755b73e81c70074436231dd62", ""),
            ("164e7693401f371bfea1b50f2805bcf8[_aOptions][1]", "ask"),
            ("164e7693401f371bfea1b50f2805bcf8[_aOptions][2]", "ask"),
            ("164e7693401f371bfea1b50f2805bcf8[_aOptions][3]", "ask"),
            ("164e7693401f371bfea1b50f2805bcf8[_aOptions][4]", "ask"),
            ("164e7693401f371bfea1b50f2805bcf8[_aOptions][5]", "ask"),
            ("164e7693401f371bfea1b50f2805bcf8[_aOptions][6]", "no"),
            ("164e7693401f371bfea1b50f2805bcf8[_aOptions][7]", "ask"),
            ("_sCaption", "icon"),
            ("_sTicketId", ticket_ids[0]),
            ("_sCaption", ""),
            ("_sTicketId", ticket_ids[1]),
            ("7bda043f4bac64379f4bd06dc716d62e", image_json),
            ("_sDescription", ""),
            ("_sVersion", str(version)),
            ("_idFileRow", str(upload.file_row_id)),
            ("_sUploadReceiptId", upload.upload_receipt_id),
            ("08feb54ae674cd1a4482aa3f54787d2b", file_json),
            ("45087a5ee344f0be04e359f2e2dd650e[]", ""),
            ("f67d32fa56c15016dc4773f4ddfbc1e8[_aUrls][]", ""),
            ("f67d32fa56c15016dc4773f4ddfbc1e8[_aDescriptions][]", ""),
            ("9909beb98df30de3d994fd98ae1b4bbc", ""),
            ("34af0296738f266d2ca374d9a3c332f5[_aDescriptions][]", ""),
            ("34af0296738f266d2ca374d9a3c332f5[_aUrls][]", ""),
            ("34af0296738f266d2ca374d9a3c332f5[_aActions][]", "< select >"),
            ("34af0296738f266d2ca374d9a3c332f5[_aActionRecommendations][]", "< select >"),
            ("f8f01eb98f600d9abd944986a9fde1b5", "0"),
            ("f23509dc8b164f4ab0381640b6dbdef3", str(version)),
            ("575127a72f0cc9f1233a8efbc5d02a3c", "0"),
            ("75ca75e67e868270e430a8b0d765f8a4", "<a href=\"https://github.com/Mackamuir/unstoppable\">Source</a>"),
            ("59b606e67af9c221b02fe473073895b4", "0"),
            ("32d8cec05b843d92bee09510f144eb19", "0"),
            ("d162a5c2c22c71f7b9ebc56a5efe8f41", "false"),
            ("7e09d546d64d500c1d5274a47706a3f6", ""),
            ("d7064f4f22bfe5c9bc9c0d94caaeed8a", "0"),
            ("185dfdab81e59e022af595b215971f60", "false"),
            ("2010d19c27afbf21f0ccd49e1b90ebd4", "true"),
            ("c8c14dd301e119ca29f9a867fcfd41ae", "false"),
            ("37f580447c23904185db5d3e5dad0170", "true"),
            ("2397e02062e66bceeb5a44621a32f559", "open"),
            ("c5cffcac18bba74c651178ab86c9e971", "enabled"),
            ("702d09c044418852c84adcfbe68d4e4f", ""),
            ("FormName", "69814328ca00a"),
        ]

        resp = self._request(
            "POST",
            f"{GB_BASE}/mods/edit/{self.mod_id}",
            data=form,
            headers={
                "origin": GB_BASE,
                "referer": f"{GB_BASE}/mods/edit/{self.mod_id}",
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "content-type": "application/x-www-form-urlencoded",
            },
            allow_redirects=True,
        )
        resp.raise_for_status()

        final_url = resp.url
        if "edit" in final_url:
            soup = BeautifulSoup(resp.text, "html.parser")
            errors = [e.get_text(strip=True) for e in soup.select(".error, .alert, [class*=error], [class*=alert]")]
            raise RuntimeError(
                f"Edit form failed (still on edit URL). errors={errors!r}, body={resp.text[:3000]!r}"
            )
        logger.info("Edit form submitted successfully: url=%s", final_url)

    def post_failure_warning(self, version: str):
        """Post a warning update to GameBanana when an update cycle fails."""
        self.authenticate()
        resp = self._request(
            "POST",
            f"{GB_API_BASE}/{self.section}/{self.mod_id}/Update",
            json={
                "_aFileRowIds": [],
                "_sVersion": version,
                "_sName": f"Warning: update may be broken ({version})",
                "_aChangeLog": [{"text": "A game update was detected but the automated update failed. The mod may be outdated or broken.", "cat": "Bug"}],
                "_sText": "<p>Warning: A game update was detected but the automated update process failed. The mod may be outdated or broken until the issue is resolved.</p>",
            },
        )
        resp.raise_for_status()
        logger.warning("Posted failure warning to GameBanana: version=%s, response=%s", version, resp.text[:500])

    def post_update(
        self,
        upload: UploadResult,
        version: str,
        added: list[str],
        removed: list[str],
        adjusted: list[str],
    ):
        """Post a mod update via the GameBanana API."""
        changelog = (
            [{"text": p, "cat": "Addition"} for p in added]
            + [{"text": p, "cat": "Removal"} for p in removed]
            + [{"text": p, "cat": "Adjustment"} for p in adjusted]
        )
        if not changelog:
            changelog = [{"text": f"Auto-update for build {version}", "cat": "Addition"}]

        resp = self._request(
            "POST",
            f"{GB_API_BASE}/{self.section}/{self.mod_id}/Update",
            json={
                "_aFileRowIds": [upload.file_row_id],
                "_sVersion": version,
                "_sName": f"Auto-update {version}",
                "_aChangeLog": changelog,
                "_sText": f"<p>Auto-update for build {version}</p>",
            },
        )
        resp.raise_for_status()
        logger.info(
            "GameBanana update posted: version=%s, +%d -%d ~%d, response=%s",
            version, len(added), len(removed), len(adjusted), resp.text[:500],
        )

    def publish(
        self,
        zip_path: Path,
        version: str,
        config: AppConfig,
        added: list[str],
        removed: list[str],
        adjusted: list[str],
    ):
        """Authenticate, upload zip, submit the edit form, and post an update."""
        self.authenticate()
        upload = self.upload_zip(zip_path)
        self.post_edit(upload, version, config)
        self.post_update(upload, version, added=added, removed=removed, adjusted=adjusted)
