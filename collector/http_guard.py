"""Credentialed GETs: no redirect is ever followed, and a body is bounded while it is read.

Every request opened here carries a provider credential. Python's default redirect handler copies
request headers, `Authorization` included, to whatever host a `Location` names, so a redirect is
refused at its first hop instead: the 3xx surfaces as an `HTTPError` carrying only its status, and
no second request is issued to any destination, same-origin or not. No supported provider endpoint
is known to redirect; one that starts to shows up as "HTTP 30x" rather than as a silent reading.

The body limit is enforced on the bytes as received. `Content-Length` only lets an oversize body be
refused early; a missing or understated one cannot get more than `limit` bytes past the loop.
Nothing here asks for or decodes a compressed body, so received bytes are the decoded bytes. An
error body is never read: the status code is the whole report.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

CHUNK = 64 * 1024


class ResponseTooLarge(ValueError):
    pass


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    # Declining here, before the stock handler parses `Location`, means a malformed destination
    # cannot raise anything but the HTTPError(3xx) the default error handler raises next.
    def http_error_302(self, req, fp, code, msg, headers):
        return None

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


# One opener for the process. Default handlers otherwise (proxy env, TLS verification untouched);
# no cookie processor is installed, so no cookie jar can carry anything between requests.
OPENER = urllib.request.build_opener(_RefuseRedirects)


def _socket_of(resp):
    """The socket under an http.client response, or None if this Python hides it."""
    raw = getattr(getattr(resp, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if hasattr(sock, "settimeout") else None


def read_bounded(url: str, headers: dict, *, timeout: float, limit: int) -> bytes:
    """GET `url` and return at most `limit` bytes of body, or raise.

    `timeout` bounds connecting and the headers, and separately the whole body read: before each
    receive the socket timeout is cut to what is left of the body deadline, so a server that
    trickles bytes, or sends one and then stalls, cannot hold the collector past it.
    """
    req = urllib.request.Request(url, headers=headers, method="GET")  # nosec B310 - callers check scheme
    try:
        resp = OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise
    with resp:
        declared = (resp.headers.get("Content-Length") or "").strip()
        if declared.isdigit() and int(declared) > limit:
            raise ResponseTooLarge(f"response declared more than {limit} bytes; not read")
        deadline = time.monotonic() + timeout
        chunks, total = [], 0
        sock = _socket_of(resp)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"response body not complete within {timeout:g}s")
            if sock is not None and resp.fp is not None:  # fp is dropped once the body is done
                sock.settimeout(remaining)
            try:
                chunk = resp.read1(min(CHUNK, limit + 1 - total))  # one receive: the deadline gets a look
            except TimeoutError as exc:
                raise TimeoutError(f"response body not complete within {timeout:g}s") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ResponseTooLarge(f"response exceeded {limit} bytes; not parsed")
            chunks.append(chunk)
    return b"".join(chunks)
