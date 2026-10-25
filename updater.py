"""Aqara integration for the same Alfred-Workflow updater used by Hubitat."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import pickle
import plistlib
import re
import stat
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

from common import AqaraError, BUNDLE_ID
from workflow import Workflow
from workflow.update import Version


REPOSITORY = "schwark/alfred-aqara"
UPDATE_KEY = "__workflow_latest_version"
MAX_DOWNLOAD = 16 * 1024 * 1024
CACHE_ERRORS = (OSError, ValueError, TypeError, KeyError, EOFError, pickle.UnpicklingError)


def correct_variant(filename):
    if not isinstance(filename, str):
        return False
    return bool(re.fullmatch(r"Aqara-\d[^/]*\.alfred(?:\d+)?workflow", filename))


class Updater:
    def __init__(self, directory, current_version=None):
        self.version = current_version or Path(__file__).with_name("version").read_text().strip()
        self.directory = Path(directory) / "workflow-updates"
        self.workflow = Workflow(update_settings={"github_slug": REPOSITORY, "version": self.version, "frequency": 1, "prereleases": False}, capture_args=False)
        # The framework owns its settings.json; Aqara owns a different settings.json.
        self.workflow.alfred_env.update(workflow_data=str(self.directory / "data"),
                                        workflow_cache=str(self.directory / "cache"),
                                        workflow_version=self.version)

    def _bind(self):
        from workflow import background, update
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        update.wf = background.wf = self.workflow

    def enabled(self):
        return self.workflow.settings.get("__workflow_autoupdate", True)

    def set_enabled(self, enabled):
        self._bind()
        self.workflow.settings["__workflow_autoupdate"] = bool(enabled)

    def available(self):
        try:
            self._bind()
            status = self.workflow.cached_data(UPDATE_KEY, max_age=0)
            if (isinstance(status, dict) and status.get("available") and status.get("download")
                    and correct_variant(status["download"].get("filename", ""))
                    and Version(status["version"]) > Version(self.version)):
                return status
        except CACHE_ERRORS:
            pass
        return None

    def schedule(self):
        """Use Hubitat's background check, with a retry delay when GitHub is down."""
        try:
            self._bind()
            if not self.enabled():
                return
            if self.workflow.settings.get("__aqara_check_version") != self.version:
                # An upgrade must not inherit the previous version's daily result
                # or failure backoff, including caches from older workflow builds.
                self.workflow.cache_data(UPDATE_KEY, None)
                self.workflow.cache_data("github-releases-" + REPOSITORY.replace("/", "-"), None)
                self.workflow.settings["__aqara_next_check"] = 0
                self.workflow.settings["__aqara_check_version"] = self.version
            if self.workflow.cached_data_fresh(UPDATE_KEY, 86400):
                return
            if time.time() < float(self.workflow.settings.get("__aqara_next_check", 0)):
                return
            self.workflow.settings["__aqara_next_check"] = time.time() + 3600
            self.workflow.check_update()
        except CACHE_ERRORS + (subprocess.SubprocessError,):
            pass  # Update infrastructure must not interrupt device search.

    def checking(self):
        try:
            self._bind()
            from workflow import background
            return background.is_running("__workflow_update_check")
        except CACHE_ERRORS:
            return False

    def install(self):
        try:
            self._bind()
            # A manual request always checks GitHub, including just-published releases.
            self.workflow.cache_data("github-releases-" + REPOSITORY.replace("/", "-"), None)
            if self.workflow.start_update():
                return "Downloading the Aqara update. Confirm the upgrade in Alfred when it opens."
            return "Aqara " + self.version + " is up to date."
        except Exception:
            raise AqaraError("Could not check or start the workflow update. Try aq workflow:update again later.") from None


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if url.scheme != "https" or url.netloc not in ("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"):
            raise AqaraError("GitHub redirected the update to an unexpected location.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_package(path, version):
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or sum(entry.file_size for entry in archive.infolist()) > 32 * 1024 * 1024:
                raise AqaraError("The downloaded workflow has an invalid archive layout.")
            for entry in archive.infolist():
                name = PurePosixPath(entry.filename)
                if name.is_absolute() or ".." in name.parts or "\\" in entry.filename or stat.S_ISLNK(entry.external_attr >> 16):
                    raise AqaraError("The downloaded workflow has an invalid archive layout.")
            if archive.getinfo("info.plist").file_size > 512 * 1024:
                raise AqaraError("The downloaded workflow has invalid metadata.")
            info = plistlib.loads(archive.read("info.plist"))
            if not isinstance(info, dict) or info.get("bundleid") != BUNDLE_ID or info.get("version") != version:
                raise AqaraError("The downloaded workflow has a different identity or version.")
    except (BadZipFile, KeyError, ValueError, TypeError, RuntimeError, plistlib.InvalidFileException):
        raise AqaraError("The download is not a valid Aqara workflow.") from None


def verified_download(download, directory=None, opener=None):
    """Alfred-Workflow download hook: verify GitHub's checksum before importing."""
    version = str(download.version)
    filename = download.filename
    prefix = "Aqara-"
    if not re.fullmatch(re.escape(prefix) + re.escape(version) + r"\.alfred(?:\d+)?workflow", filename):
        raise AqaraError("The release asset is not an Aqara workflow.")
    url = urlsplit(download.url)
    paths = ["/" + REPOSITORY + "/releases/download/" + prefix + version + "/" + filename for prefix in ("v", "")]
    if url.scheme != "https" or url.netloc != "github.com" or url.path not in paths or url.query or url.fragment:
        raise AqaraError("The update is not from the workflow's GitHub repository.")
    if not isinstance(download.digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", download.digest):
        raise AqaraError("The release does not include a SHA-256 checksum.")
    if type(download.size) is not int or not 0 < download.size <= MAX_DOWNLOAD:
        raise AqaraError("The workflow download has an unexpected size.")
    fallback = Path.home() / "Library/Caches/com.runningwithcrayons.Alfred/Workflow Data" / BUNDLE_ID
    destination_dir = Path(directory or os.environ.get("alfred_workflow_cache") or fallback) / "downloads"
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=str(destination_dir))
    try:
        digest, size = hashlib.sha256(), 0
        request = Request(download.url, headers={"User-Agent": "Alfred-Aqara-Updater", "Accept": "application/octet-stream"})
        opener = opener or build_opener(GitHubRedirects())
        with os.fdopen(fd, "wb") as stream, opener.open(request, timeout=20) as response:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > download.size:
                    raise AqaraError("The workflow download is larger than expected.")
                digest.update(chunk)
                stream.write(chunk)
        if size != download.size or "sha256:" + digest.hexdigest() != download.digest:
            raise AqaraError("The workflow download failed its integrity check. Try again later.")
        _validate_package(temporary, version)
        destination = destination_dir / filename
        os.replace(temporary, destination)
        return destination
    except (OSError, URLError):
        raise AqaraError("Could not download the workflow update. Try aq workflow:update again later.") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
