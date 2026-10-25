"""mitmproxy guard for read-only mobile research; excluded from the workflow.

This blocks HTTP writes to Aqara services. It is not a firewall for native
Matter/Bluetooth transports; do not activate device controls during capture.
"""

from urllib.parse import unquote, urlsplit


READ_POSTS = {
    "/user/guard-code/login", "/res/query", "/res/query/by/resourceId",
    "/devex/device/pwd/state/query",
    "/app/qlink/trait/read",
}
MUTATING_SEGMENTS = {"write", "add", "del", "delete", "remove", "run",
                     "execute", "set", "update", "bind", "unbind"}
PREFIX = "/app/v1.0/lumi"


def permitted(method, host, path):
    host = host.lower().rstrip(".")
    if not any(host == domain or host.endswith("." + domain)
               for domain in ("aqara.com", "ankasa.cn")):
        return True
    path = unquote(urlsplit(path).path)
    if path == PREFIX + "/devex/lock/pwd":
        return False
    if method.upper() in ("GET", "HEAD"):
        return not MUTATING_SEGMENTS.intersection(path.lower().split("/"))
    return method.upper() == "POST" and path.startswith(PREFIX) and path[len(PREFIX):] in READ_POSTS


def request(flow):
    if not permitted(flow.request.method, flow.request.host, flow.request.path):
        from mitmproxy import http
        flow.response = http.Response.make(
            403, b'{"code":95001,"message":"Blocked by read-only research proxy"}',
            {"Content-Type": "application/json"})
