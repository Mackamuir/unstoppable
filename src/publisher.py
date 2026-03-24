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

CHUNK_SIZE = 1024 * 1024  # 1 MB
GB_BASE = "https://gamebanana.com"
GB_API_BASE = "https://gamebanana.com/apiv11"
GB_UPLOAD_URL = f"{GB_BASE}/responders/jfuare"

DESCRIPTION_PREFIX = "Unstoppable is a mod that holds vanilla files"


@dataclass
class UploadResult:
    file_row_id: int
    upload_receipt_id: str
    filename: str


class GameBananaPublisher:
    def __init__(self, username: str, password: str, mod_id: int):
        self.username = username
        self.password = password
        self.mod_id = mod_id
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

    def get_published_version(self) -> str | None:
        """Query GameBanana for the currently published version of the mod."""
        resp = self._request(
            "GET",
            f"{GB_API_BASE}/Mod/{self.mod_id}",
            params={"_csvProperties": "_sVersion"},
        )
        resp.raise_for_status()
        if not resp.text:
            logger.warning("GameBanana version API returned empty body (status=%d)", resp.status_code)
            return None
        data = resp.json()
        version = data.get("_sVersion")
        logger.info("GameBanana published version: %s", version)
        return version

    # ── Edit page scraping ────────────────────────────────────────────────

    def _get_edit_page(self) -> str:
        """Fetch the edit page HTML."""
        edit_url = f"{GB_BASE}/mods/edit/{self.mod_id}"
        resp = self._request("GET", edit_url, headers={"accept": "text/html"})
        resp.raise_for_status()
        return resp.text

    def _get_upload_fields(self, html: str) -> tuple[str, str, str]:
        """Extract sdpid, files field hash, and image field hash from the edit page JS.

        The files upload uses /responders/jfuare with ``"sdpid":"<token>"``.
        The image upload uses /responders/jfu with ``"d":"<token>"``.
        Returns (sdpid, files_field_name, image_field_name).
        """
        # Files field: the _FileInput whose own formData block contains "sdpid"
        match = re.search(
            r'#([a-f0-9]{32})_FileInput"\)\.fileupload\(\{\s*formData:\s*\{\s*"sdpid"\s*:\s*"([a-f0-9]{32})"',
            html, re.DOTALL,
        )
        if not match:
            # Fallback: looser search
            match = re.search(r'"sdpid"\s*:\s*"([a-f0-9]{32})"', html)
            if match:
                sdpid = match.group(1)
                logger.warning("Found sdpid %s but could not determine field names", sdpid)
                return sdpid, "", ""
            raise RuntimeError(
                f"Could not find sdpid in edit page HTML ({len(html)} chars)"
            )

        files_field = match.group(1)
        sdpid = match.group(2)

        # Image field: the _FileInput whose formData block uses "d" (not "sdpid")
        image_field = ""
        img_match = re.search(
            r'#([a-f0-9]{32})_FileInput"\)\.fileupload\(\{\s*formData:\s*\{\s*"d"\s*:',
            html, re.DOTALL,
        )
        if img_match:
            image_field = img_match.group(1)

        logger.info(
            "Found sdpid: %s, files field: %s, image field: %s",
            sdpid, files_field, image_field,
        )
        return sdpid, files_field, image_field

    # ── Chunked file upload ───────────────────────────────────────────────

    def upload_zip(self, zip_path: Path, sdpid: str) -> UploadResult:
        """Upload zip file in 1 MB chunks. Returns UploadResult with file row ID and receipt."""
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

    # ── Dynamic form scraping and submission ──────────────────────────────

    def _scrape_form(self, html: str) -> list[tuple[str, str]]:
        """Scrape all form fields from the edit page HTML.

        Returns a list of (name, value) tuples preserving order and duplicates.
        """
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form")
        if not form:
            raise RuntimeError("No <form> found in edit page HTML")

        fields: list[tuple[str, str]] = []

        for el in form.find_all(["input", "textarea", "select"]):
            name = el.get("name")
            if not name:
                continue
            # Browsers don't submit disabled fields
            if el.has_attr("disabled"):
                continue

            if el.name == "textarea":
                value = el.string or ""
                fields.append((name, value))
            elif el.name == "select":
                if el.has_attr("multiple"):
                    # Multi-select: add each selected option; skip if none selected
                    for opt in el.find_all("option", selected=True):
                        fields.append((name, opt.get("value", "")))
                else:
                    selected = el.find("option", selected=True)
                    value = selected.get("value", "") if selected else ""
                    fields.append((name, value))
            elif el.name == "input":
                input_type = (el.get("type") or "text").lower()
                if input_type in ("submit", "button", "file", "image"):
                    continue
                if input_type == "checkbox":
                    if el.has_attr("checked"):
                        fields.append((name, el.get("value", "on")))
                elif input_type == "radio":
                    if el.has_attr("checked"):
                        fields.append((name, el.get("value", "")))
                else:
                    fields.append((name, el.get("value", "")))

        logger.info("Scraped %d form fields from edit page", len(fields))
        return fields

    def _find_ownership_fields(self, html: str) -> list[tuple[str, str]]:
        """Extract ownership/author fields from the page JS.

        These are JS-generated. The prefix hash appears near 'group_name' or 'author'.
        """
        match = re.search(r'var\s+g_sInputName\s*=\s*"([a-f0-9]{32})"', html)
        if not match:
            logger.warning("Could not find g_sInputName for ownership fields")
            return []
        prefix = match.group(1)
        logger.info("Found ownership field prefix: %s", prefix)
        return [
            (f"{prefix}[1][group_name]", "Developer"),
            (f"{prefix}[1][author_userids][]", "5279634"),
            (f"{prefix}[1][author_names][]", "mack.wtf"),
            (f"{prefix}[1][author_offsite_urls][]", ""),
            (f"{prefix}[1][author_roles][]", "dude who made it"),
        ]

    def _find_ticket_ids(self, html: str) -> list[str]:
        """Find image ticket IDs from the page JS."""
        ticket_ids = re.findall(
            r'[Tt]icket[Ii]d["\']?\s*[:=,]\s*["\']([a-f0-9]{32})["\']', html
        )
        logger.info("Found %d ticket IDs: %s", len(ticket_ids), ticket_ids)
        return ticket_ids

    def _find_js_template_fields(self, html: str) -> list[tuple[str, str]]:
        """Extract form field defaults from JS template strings.

        Some fields (embedded media, alternate sources, requirements) are built
        by jQuery ``$('<li>...')`` templates and are invisible to BeautifulSoup.
        """
        fields: list[tuple[str, str]] = []

        # Embedded media URL input
        match = re.search(
            r'name="([a-f0-9]{32})\[\]"[^>]*placeholder="URL or Embed Code', html,
        )
        if match:
            fields.append((f"{match.group(1)}[]", ""))

        # Alternate file sources (URL + Description pair)
        match = re.search(r'name="([a-f0-9]{32})\[_aUrls\]\[\]"', html)
        if match:
            prefix = match.group(1)
            fields.append((f"{prefix}[_aUrls][]", ""))
            fields.append((f"{prefix}[_aDescriptions][]", ""))

        # Requirements (Description, URL, Action, ActionRecommendation)
        match = re.search(
            r'name="([a-f0-9]{32})\[_aDescriptions\]\[\]"[^>]*placeholder="Requirement',
            html,
        )
        if not match:
            match = re.search(
                r'placeholder="Requirement[^"]*"[^>]*name="([a-f0-9]{32})\[_aDescriptions\]\[\]"',
                html,
            )
        if match:
            prefix = match.group(1)
            fields.append((f"{prefix}[_aDescriptions][]", ""))
            fields.append((f"{prefix}[_aUrls][]", ""))
            fields.append((f"{prefix}[_aActions][]", "< select >"))
            fields.append((f"{prefix}[_aActionRecommendations][]", "< select >"))

        logger.info("Found %d JS template fields", len(fields))
        return fields

    def post_edit(self, upload: UploadResult, version: str, description: str,
                  files_json_name: str, image_json_name: str):
        """Scrape the edit page form, override key fields, and submit.

        The files JSON blob, individual file/image fields, and ownership fields
        are JS-constructed (not in the HTML form). We extract field names from
        the page's JS and construct them ourselves.
        """
        html = self._get_edit_page()
        fields = self._scrape_form(html)
        soup = BeautifulSoup(html, "html.parser")

        # Find the description textarea name by matching content
        desc_name = None
        for name, value in fields:
            if value and DESCRIPTION_PREFIX in value:
                desc_name = name
                break

        # Find the version field name — it's the input inside #Version
        version_section = soup.find(id="Version")
        version_field_name = None
        if version_section:
            version_input = version_section.find("input")
            if version_input:
                version_field_name = version_input.get("name")
        logger.info("Description field: %s, Version field: %s", desc_name, version_field_name)

        # Build file entries — single entry replacing the old file
        file_entries = [[
            {"name": "_sDescription", "value": ""},
            {"name": "_sVersion", "value": str(version)},
            {"name": "_idFileRow", "value": str(upload.file_row_id)},
            {"name": "_sUploadReceiptId", "value": upload.upload_receipt_id},
        ]]

        # Find image ticket IDs from JS
        ticket_ids = self._find_ticket_ids(html)

        # Build image JSON entries (with real ticket IDs in the blob)
        image_json_entries = []
        for i, tid in enumerate(ticket_ids):
            image_json_entries.append({"name": "_sCaption", "value": "icon" if i == 0 else ""})
            image_json_entries.append({"name": "_sTicketId", "value": tid})

        # Build individual image fields (empty ticket IDs — browser behaviour)
        image_individual_fields = []
        for i in range(len(ticket_ids)):
            image_individual_fields.append(("_sCaption", "icon" if i == 0 else ""))
            image_individual_fields.append(("_sTicketId", ""))

        # Find ownership fields (JS-generated)
        ownership_fields = self._find_ownership_fields(html)

        # Find JS template fields (embedded media, alt sources, requirements)
        js_template_fields = self._find_js_template_fields(html)

        logger.info("Image JSON field: %s, Files JSON field: %s", image_json_name, files_json_name)

        # Build the form data
        files_json_value = json.dumps(file_entries)
        image_json_value = json.dumps(image_json_entries) if image_json_entries else "[]"
        form_data: list[tuple[str, str]] = []

        # Track where to insert ownership (after the first field with value "true")
        first_true_index = next(
            (i for i, (_, v) in enumerate(fields) if v == "true"), None,
        )

        for i, (name, value) in enumerate(fields):
            if desc_name and name == desc_name:
                form_data.append((name, description))
            elif version_field_name and name == version_field_name:
                form_data.append((name, str(version)))
            elif image_json_name and name == image_json_name:
                # Individual image fields, then the JSON blob
                for fname, fval in image_individual_fields:
                    form_data.append((fname, fval))
                form_data.append((name, image_json_value))
            elif files_json_name and name == files_json_name:
                # Individual file fields, then the JSON blob
                for file_entry in file_entries:
                    for field in file_entry:
                        form_data.append((field["name"], field["value"]))
                form_data.append((name, files_json_value))
                # JS template fields go right after the files JSON field
                for tname, tval in js_template_fields:
                    form_data.append((tname, tval))
            else:
                form_data.append((name, value))

            # Insert ownership fields after the first "true" field
            if first_true_index is not None and i == first_true_index:
                for oname, ovalue in ownership_fields:
                    form_data.append((oname, ovalue))

        edit_url = f"{GB_BASE}/mods/edit/{self.mod_id}"
        logger.info("Submitting edit form with %d fields", len(form_data))
        for name, value in form_data:
            logger.debug("  form: %s = %r", name, value[:200] if len(value) > 200 else value)
        resp = self._request(
            "POST",
            edit_url,
            data=form_data,
            headers={
                "origin": GB_BASE,
                "referer": edit_url,
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "content-type": "application/x-www-form-urlencoded",
            },
            allow_redirects=True,
        )
        resp.raise_for_status()

        logger.info(
            "Edit POST response: status=%d, url=%s, history=%s, headers=%s, body_len=%d, body=%r",
            resp.status_code, resp.url,
            [r.status_code for r in resp.history],
            dict(resp.headers),
            len(resp.text),
            resp.text[:5000],
        )

        final_url = resp.url
        if "edit" in final_url:
            raise RuntimeError(
                f"Edit form failed (still on edit URL: {final_url})"
            )
        logger.info("Edit form submitted successfully: url=%s", final_url)

    # ── Description builder ───────────────────────────────────────────────

    @staticmethod
    def _build_description(config: AppConfig) -> str:
        """Build the mod description HTML from config."""
        file_list_items = [
            f"<li>Deadlock/{config.source_vpk_path}:{pattern}</li>"
            for pattern in config.tracked_vpk_files
        ] + [
            f"<li>{pattern}</li>"
            for pattern in config.tracked_loose_files
        ]
        file_list_html = "<ul>" + "".join(file_list_items) + "</ul>"

        return (
            "Unstoppable is a mod that holds vanilla files so that other mods are unable to overwrite them.<br>"
            "Tons of mods try to all replace the same files, or replace files and break UI.<br><br>"
            "The following files are included in this mod:<br><br>"
            + file_list_html
            + "<br>"
            "This mod should be placed at Deadlock/game/citadel/addons/pak01_dir.vpk, or enabled first in the mod manager. "
            "If you believe it is breaking other mod (lol) then place unstoppable at pak02 and the first mod at pak01.<br><br>"
            "If you have any files you wish for this mod to track, feel free to leave a comment.<br><br>"
            "In theory this mod will always be current.<br><br>"
            "Due to the auto-uploader being kinda /unstable/ all releases are also mirrored on <a href='https://github.com/Mackamuir/unstoppable/releases' target='_blank'>GitHub</a>"
        )

    # ── API-based methods (no browser needed) ─────────────────────────────

    def post_failure_warning(self, version: str) -> int:
        """Post a warning update to GameBanana when an update cycle fails. Returns the update _idRow."""
        self.authenticate()
        resp = self._request(
            "POST",
            f"{GB_API_BASE}/Mod/{self.mod_id}/Update",
            json={
                "_aFileRowIds": [],
                "_sVersion": version,
                "_sName": f"Warning: update may be broken ({version})",
                "_sText": "Warning: A game update was detected but the automated update process failed. The mod may be outdated or broken until the issue is resolved.",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        update_id = data.get("_idRow")
        logger.warning(
            "Posted failure warning to GameBanana: version=%s, update_id=%s, response=%s",
            version, update_id, resp.text[:500],
        )
        return update_id

    def delete_update(self, update_id: int):
        """Delete a GameBanana update post (e.g. a previously posted failure warning)."""
        resp = self._request(
            "DELETE",
            f"{GB_API_BASE}/Update/{update_id}",
            json={
                "_idReasonRow": "1",
                "_bHideReason": True,
                "_sNotes": "<p>Hidden</p>",
            },
        )
        resp.raise_for_status()
        logger.info("Deleted GameBanana update: update_id=%d, response=%s", update_id, resp.text[:500])

    def notify_deadlockmods(self):
        """Notify deadlockmods.app that the mod has been updated."""
        url = f"https://api.deadlockmods.app/api/v2/sync/{self.mod_id}"
        try:
            resp = self._request("POST", url)
            logger.info("deadlockmods sync: status=%d, body=%r", resp.status_code, resp.text[:200])
        except Exception:
            logger.warning("deadlockmods sync request failed", exc_info=True)

    # ── Main entry point ──────────────────────────────────────────────────

    def publish(
        self,
        zip_path: Path,
        version: str,
        config: AppConfig,
    ):
        """Authenticate, upload zip, submit the edit form, and notify."""
        description = self._build_description(config)
        self.authenticate()

        # Fetch edit page once for sdpid, files field name, and image field name
        html = self._get_edit_page()
        sdpid, files_json_name, image_json_name = self._get_upload_fields(html)

        upload = self.upload_zip(zip_path, sdpid)
        self.post_edit(upload, version, description, files_json_name, image_json_name)
        self.notify_deadlockmods()
