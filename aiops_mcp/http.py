"""
Stdlib HTTP, shaped so failures arrive as the right kind of error.

Lifted from tools/scenario_runner.py, which already has working helpers against
both backends. COPIED rather than imported, deliberately: scenario_runner.py is
verified, committed Session 2 code that produces ground_truth.jsonl, and an
import would let a refactor here break the thing that generates the labels
everything downstream is scored against. That is the same trade as engineering
log entry 62 (three copies of the chaos classes rather than a shared module) and
it is recorded rather than assumed away.

WHAT CHANGED IN THE COPY, and why
---------------------------------
scenario_runner's `http()` returns the exception's *class name* in the status
position on failure, so a caller sees ("URLError", {}, "...") and has to
string-match to find out what went wrong. That is fine for a script whose
failure mode is "print it and stop". It is not fine here: distinguishing "nobody
is listening" from "Prometheus rejected the query" IS the contract this session
is being judged on. So this version raises typed exceptions and reserves the
return value for genuine HTTP responses.
"""

import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from .util import BackendError, BackendTimeout, BackendUnreachable


def get_json(base_url, path, params, timeout, target):
    """GET JSON, or raise a typed ToolError.

    `target` is the human name of the backend ("prometheus"/"jaeger") and is
    carried into the error so the model can tell WHICH backend is down. With two
    backends in play, "connection refused" alone is not actionable.
    """
    url = base_url + path
    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(url, method="GET",
                                     headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # The backend answered and refused. Prometheus puts a genuinely useful
        # message in the body for a malformed query ("parse error at char 12"),
        # so it is forwarded rather than discarded -- but bounded, because an
        # error body is still untrusted input on its way to a context window.
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        detail = _prom_error_message(body) or body or exc.reason
        raise BackendError(f"{target} returned HTTP {exc.code}: {detail}",
                           target=target, http_status=exc.code)
    except socket.timeout:
        raise BackendTimeout(f"{target} did not answer within {timeout:.1f}s",
                             target=target)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            raise BackendTimeout(f"{target} did not answer within {timeout:.1f}s",
                                 target=target)
        # Nothing listening. On Windows this is WinError 10061; the message is
        # kept verbatim because it is the one the operator will search for.
        raise BackendUnreachable(f"cannot reach {target} at {base_url}: {reason}",
                                 target=target)
    except (OSError, ValueError) as exc:
        raise BackendUnreachable(f"cannot reach {target} at {base_url}: {exc}",
                                 target=target)

    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        # A 200 whose body is not JSON means we are talking to something that is
        # not the API we think it is -- a proxy login page, or the wrong port.
        raise BackendError(f"{target} returned a 200 that is not JSON: {exc}",
                           target=target)


def _prom_error_message(body):
    """Pull Prometheus's own error text out of its JSON error envelope.

    Prometheus answers a bad query with 400 and
    {"status":"error","errorType":"bad_data","error":"..."} -- the `error` field
    is the parser message and is far more useful than the status line.
    """
    try:
        parsed = json.loads(body)
    except Exception:
        return None
    if isinstance(parsed, dict) and parsed.get("error"):
        return str(parsed["error"])[:300]
    return None


def reachable(base_url, path, timeout=3.0):
    """Cheap liveness probe. Returns (ok, detail).

    A REAL HTTP request, on purpose. `docker compose ps` reporting "healthy"
    says nothing about reachability from the host -- Session 1 lost time to a
    healthy-but-unreachable container, and Session 2 to Grafana answering 000 for
    25s while ps said "Up 2 minutes" because Grafana has no healthcheck at all.
    Absence of alarming health information reads as fine, and is not.
    """
    try:
        request = urllib.request.Request(base_url + path, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # Answering at all proves something is listening and speaking HTTP,
        # which is the question being asked here.
        return True, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
