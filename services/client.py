"""Tiny stdlib HTTP client (no extra dependency)."""
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class HttpError(Exception):
    def __init__(self, status, body=""):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


# CLAUDE FIX: connection resets while reading, truncated/odd HTTP, and HTML-instead-of-JSON replies used to
# escape as OSError / HTTPException / ValueError, which callers (they catch HttpError) did not handle.
_NET = (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException)


def get_json(url, params=None, headers=None, timeout=20, retries=3):
    """Returns (data, lowercase_headers). Retries 429/5xx/timeouts with backoff. Raises only HttpError
    (status 0 = network problem, 200 = the reply was not JSON)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", **(headers or {})})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                hdr = {k.lower(): v for k, v in r.headers.items()}
                raw = r.read().decode("utf-8")
            try:
                return json.loads(raw), hdr
            except ValueError:
                raise HttpError(200, "not JSON: " + raw[:120])
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if e.code in (429, 500, 502, 503, 504) and i < retries - 1:
                last = HttpError(e.code, body)
                time.sleep(1.5 * (i + 1))
                continue
            raise HttpError(e.code, body)
        except _NET as e:
            last = HttpError(0, str(e))
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
        except ValueError as e:                       # undecodable bytes
            raise HttpError(200, f"not JSON: {e}")
    raise last or HttpError(0, "no attempts made")


def post_json(url, payload, timeout=40):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "ignore"))
    except _NET as e:
        raise HttpError(0, str(e))
    except ValueError as e:
        raise HttpError(200, f"not JSON: {e}")
