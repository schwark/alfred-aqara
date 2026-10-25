"""Obtain shared app constants automatically; never send account data to GitHub."""

import hashlib
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from cloud_transport import AppIdentity, CloudAPIError, NoRedirects

REFERENCE_URL = ("https://raw.githubusercontent.com/home-assistant-plugins/aquara-local/"
                 "4fc274688cd40774307549172c9ae1db073c92ca/custom_components/aquara_local/const.py")
REFERENCE_SHA256 = "cc0ebf8837784387b7e0f3d43b27b6753f6a4104db7bc415480396cbc6283109"


def public_identity(keychain, opener=None):
    try:
        cached = json.loads(keychain.get() or "{}")
        if isinstance(cached, dict) and cached.get("reference_sha256") == REFERENCE_SHA256:
            return AppIdentity.from_payload(cached.get("identity"))
    except (ValueError, TypeError, CloudAPIError):
        pass  # A malformed cache is replaced from the verified public source.
    opener = opener or build_opener(NoRedirects())
    try:
        with opener.open(Request(REFERENCE_URL, headers={"User-Agent": "Alfred-Aqara-Setup"}), timeout=20) as response:
            raw = response.read(65537)
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != REFERENCE_SHA256:
            raise CloudAPIError("The public Cloud setup reference failed its checksum or size check. No account credentials were sent.")
        identity = AppIdentity.from_reference_text(raw.decode("utf-8"))
    except HTTPError as exc:
        exc.close()
        raise CloudAPIError("Cannot download Cloud setup from GitHub. Check your connection and retry aq login.") from None
    except (OSError, URLError, UnicodeError):
        raise CloudAPIError("Cannot download Cloud setup from GitHub. Check your connection and retry aq login.") from None
    keychain.set(json.dumps({"reference_sha256": REFERENCE_SHA256, "identity": identity.payload()}))
    return identity
