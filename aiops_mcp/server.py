"""
The MCP server itself: JSON-RPC 2.0 over stdio, hand-rolled on the stdlib.

WHAT MCP ACTUALLY IS
--------------------
Three things, and no more:

  1. JSON-RPC 2.0 as the message format. Requests carry {jsonrpc, id, method,
     params}; responses carry {jsonrpc, id, result} or {jsonrpc, id, error}. A
     message with NO id is a notification and MUST NOT be answered.
  2. A fixed method vocabulary. This server implements the five that matter for
     a tools-only server: initialize, notifications/initialized, tools/list,
     tools/call, ping.
  3. A handshake. Client sends `initialize` with its protocol version and
     capabilities; server replies with its own version, capabilities and
     serverInfo; client then sends the `notifications/initialized` notification.
     Only after that is the session live.

That is the whole protocol. Everything else -- resources, prompts, sampling,
roots -- is optional surface this server declares it does not have.

WHY HAND-ROLLED RATHER THAN THE OFFICIAL SDK
--------------------------------------------
The SDK is the right default and would be about 40 lines of decorators. It was
declined here for one reason: the deliverable of this session is being able to
explain the protocol, and a decorator explains nothing. The framing, the
handshake ordering, and the two error channels below are visible in this file
because they were written out, and that is the point.

The cost is real and stated: no Streamable-HTTP transport, no OAuth, no
progress notifications, no resource subscriptions, and any protocol revision
must be tracked by hand. The moment a second transport is needed, this file
should be replaced by the SDK rather than grown. Also stated: this was
timeboxed, with the SDK as the pre-agreed fallback.

THE TWO ERROR CHANNELS -- the load-bearing protocol detail
----------------------------------------------------------
This is the part that is easy to get wrong and expensive to get wrong.

  * A JSON-RPC `error` object (-32700 parse, -32600 invalid request, -32601
    method not found, -32602 invalid params) is a PROTOCOL failure. It is
    delivered to the CLIENT. The model never sees it. It means "this message
    was not a valid call".

  * A successful JSON-RPC response whose result carries isError: true is a TOOL
    failure. It is delivered to the MODEL. It means "the call was valid, and
    here is why it did not work".

"Prometheus is down" is emphatically the second kind. Reporting it as a JSON-RPC
error would make it invisible to the only party capable of reacting -- the model
would see a tool that silently did nothing, and would most likely invent an
answer or retry the identical call forever. Reporting it as isError=true lets it
say "Prometheus is unreachable" and stop. Every ToolError raised anywhere in
this package lands in that second channel.

STDOUT IS THE WIRE
------------------
Under stdio transport the client spawns this process and reads frames from its
stdout. One stray print() -- ours, or one buried in a library -- injects a
non-JSON line and the client drops the connection with a parse error pointing at
nothing useful. Rather than promising not to print, __main__ below takes the real
stdout into a private handle and rebinds sys.stdout to stderr, so an accidental
print is harmless by construction.

Framing is newline-delimited JSON. It resembles the Language Server Protocol but
has NO Content-Length headers -- one JSON object per line, no embedded newlines.
"""

import io
import json
import sys
import traceback

from . import DEFAULT_TRACES, MAX_TRACES, SERVER_NAME, SERVER_VERSION, SERVICES
from .metrics import METRICS, PERCENTILES, query_metrics
from .status import telemetry_status
from .traces import get_trace, query_traces
from .util import BadArgument, ToolError

# The revision this server was written against, plus older ones it can still
# speak. If a client asks for something unknown, the spec says answer with the
# version we DO support and let the client decide whether to continue -- not to
# fail the handshake.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC 2.0 reserved codes. Only these four can occur here.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# Tool schemas
#
# Every schema below is resident in the model's context on EVERY turn of every
# conversation where this server is connected. That is the cost model that
# matters: description text is not paid once, it is paid per turn, forever. So
# the descriptions state what the tool does, the closed vocabulary (which is
# what stops the model guessing a metric name and getting a plausible empty
# answer), and nothing else. Rationale, worked examples and failure taxonomies
# live in the module docstrings, where a human reads them and the model does
# not pay for them.
#
# `enum` is doing real work rather than decorating: an enum is enforced by the
# client before the call is made, so a wrong metric name costs zero round trips
# and cannot reach Prometheus at all. It also means the description does not
# have to spend tokens listing the values in prose -- they are in the schema
# once, structurally.
# ---------------------------------------------------------------------------

_TIME_PROPS = {
    "window": {
        "type": "string",
        "description": "Relative lookback like '15m', '2h', '1d'. Default 15m. "
                       "Mutually exclusive with start/end.",
    },
    "start": {
        "type": "string",
        "description": "Absolute UTC ISO-8601 start, e.g. 2026-08-09T20:17:31Z. "
                       "Timezone is required.",
    },
    "end": {
        "type": "string",
        "description": "Absolute UTC ISO-8601 end. Requires start. Defaults to now.",
    },
}

TOOLS = [
    {
        "name": "query_metrics",
        "description": (
            "Prometheus metrics for the gateway/orders/inventory services: "
            "latency percentiles, request rate, error rate, error ratio, and "
            "the inventory DB connection pool. Returns window-wide percentiles "
            "plus a downsampled series. Health and chaos-control routes are "
            "excluded. Retention is 24h."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": list(METRICS),
                    "description": "latency=response time percentiles (ms). "
                                   "request_rate=req/s. error_rate=5xx req/s. "
                                   "error_ratio=5xx as % of requests. "
                                   "db_pool=inventory HikariCP connections.",
                },
                "service": {
                    "type": "string",
                    "enum": list(SERVICES) + ["all"],
                    "description": "Default 'all', which returns one row per service.",
                },
                "percentile": {
                    "type": "integer",
                    "enum": list(PERCENTILES),
                    "description": "Which percentile the time series shows. "
                                   "Default 95. p50/p95/p99 are always returned.",
                },
                **_TIME_PROPS,
            },
            "required": ["metric"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_traces",
        "description": (
            "Sampled distributed traces, folded into per-service self-time. Use "
            "when metrics show WHICH service is slow and you need to know WHERE "
            "the time goes. self_time excludes waiting on downstream calls, so "
            "it separates 'this service is slow' from 'this service is blocked "
            "on a slow dependency' -- two cases that look identical in latency "
            "metrics. Returns aggregates plus compact per-trace rows, never raw "
            "spans. Jaeger keeps 72h."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "enum": list(SERVICES),
                    "description": "The service that STARTED the trace, i.e. its "
                                   "entry point -- not every service it touches.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TRACES,
                    "description": f"Traces to sample. Default {DEFAULT_TRACES}, "
                                   f"capped at {MAX_TRACES}.",
                },
                "min_duration_ms": {
                    "type": "number",
                    "description": "Keep only traces at least this slow.",
                },
                "errors_only": {
                    "type": "boolean",
                    "description": "Keep only traces containing a 5xx or error span.",
                },
                **_TIME_PROPS,
            },
            "required": ["service"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_trace",
        "description": (
            "One trace as an indented span tree, showing each span's duration, "
            "its own self-time, and its offset from trace start. Use after "
            "query_traces returns a trace_id worth looking at. A child starting "
            "long after its parent began indicates the parent was waiting "
            "before it called out."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string",
                             "description": "Hex trace id from query_traces."},
            },
            "required": ["trace_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "telemetry_status",
        "description": (
            "Whether Prometheus and Jaeger are reachable, how far back each can "
            "be queried, and which services are currently reporting. Call this "
            "when a query returns empty or fails, to tell an outage apart from a "
            "genuinely quiet system."
        ),
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    },
]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def call_tool(name, arguments):
    """Route a tools/call to the implementation. Raises ToolError on failure."""
    if not isinstance(arguments, dict):
        raise BadArgument(
            f"arguments must be an object, got {type(arguments).__name__}")

    if name == "query_metrics":
        return query_metrics(
            metric=arguments.get("metric"),
            service=arguments.get("service", "all"),
            window=arguments.get("window"),
            start=arguments.get("start"),
            end=arguments.get("end"),
            percentile_=arguments.get("percentile", 95),
        )

    if name == "query_traces":
        return query_traces(
            service=arguments.get("service"),
            window=arguments.get("window"),
            start=arguments.get("start"),
            end=arguments.get("end"),
            limit=arguments.get("limit", DEFAULT_TRACES),
            min_duration_ms=arguments.get("min_duration_ms"),
            errors_only=bool(arguments.get("errors_only", False)),
        )

    if name == "get_trace":
        return get_trace(trace_id=arguments.get("trace_id"))

    if name == "telemetry_status":
        return telemetry_status()

    # Distinct from METHOD_NOT_FOUND: the method (tools/call) exists and was
    # well-formed, so this is a tool-level failure the model should see and
    # recover from by picking a real tool name.
    # bad_argument rather than the generic internal kind: this is the caller's
    # mistake and is fixable by the caller, which is exactly what that kind
    # means. Reporting it as `internal` would tell the model the server is
    # broken and there is nothing to try -- the opposite of the truth.
    raise BadArgument(f"unknown tool {name!r}",
                      known=[t["name"] for t in TOOLS])


def render(payload):
    """Serialise a tool result into MCP content blocks.

    JSON, not prose, for everything numeric. A table of numbers rendered as
    English costs more tokens and destroys the structure the model needs to
    compare two services -- it would have to parse sentences back into numbers.
    Pre-rendered text is the right choice for tree structure (see get_trace) and
    the wrong one here.

    The `tree` key is the one exception, and it is emitted RAW. Wrapping it in
    JSON would escape every newline as \\n and every quote as \\", turning an
    indented tree into one long unreadable line and paying for the escapes --
    JSON-encoding text that is already the final output makes it both bigger and
    worse.

    separators=(",", ":") removes the space after every comma and colon.
    Cosmetic to read, not cosmetic to pay for: on a full three-service latency
    response it is around 8% of the bytes, for zero information.

    NOT declaring outputSchema is deliberate. The spec requires a server that
    declares one to ALSO emit backward-compatible text content mirroring it,
    which means the same payload is serialised twice into the same response --
    double the tokens for a schema the model can infer from one look at the JSON.
    """
    if isinstance(payload, dict) and "tree" in payload:
        return {"content": [{"type": "text", "text": payload["tree"]}],
                "isError": False}
    return {
        "content": [{"type": "text",
                     "text": json.dumps(payload, separators=(",", ":"))}],
        "isError": False,
    }


def render_error(exc):
    """A tool failure, addressed to the MODEL -- not a JSON-RPC protocol error.

    isError=true is what makes the difference between the model saying
    "Prometheus is unreachable" and the model inventing a latency number.
    """
    body = exc.payload() if isinstance(exc, ToolError) else {
        "kind": "internal", "detail": f"{type(exc).__name__}: {exc}"}
    return {
        "content": [{"type": "text",
                     "text": json.dumps({"error": body}, separators=(",", ":"))}],
        "isError": True,
    }


def handle(message, state):
    """One request in, one response dict out (or None for a notification)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return error_response(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")

    method = message.get("method")
    params = message.get("params") or {}
    ident = message.get("id")
    # Absence of an id -- not a null id -- is what makes a message a
    # notification. `"id": null` is a malformed request, so `is None` alone
    # would be the wrong test.
    is_notification = "id" not in message

    if method == "initialize":
        requested = params.get("protocolVersion")
        state["client"] = params.get("clientInfo", {})
        return result_response(ident, {
            # Echo the client's version when we can speak it; otherwise state
            # ours and let the client decide. Failing the handshake over a
            # version mismatch is the wrong call -- the client may well be able
            # to downgrade.
            "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS
                               else PROTOCOL_VERSION,
            # Declared honestly. Announcing resources or prompts we do not
            # implement would make the client list them and then fail on use.
            # listChanged=False because this tool set is fixed at startup.
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "notifications/initialized":
        state["ready"] = True
        return None                       # notification: answering it is a bug

    if method == "ping":
        # Spec: an empty result object. Liveness only.
        return result_response(ident, {})

    if method == "tools/list":
        return result_response(ident, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            # Malformed CALL, not a failed tool -- the request itself is
            # invalid, so this belongs in the client-facing channel.
            return error_response(ident, INVALID_PARAMS,
                                  "params.name must be a string")
        try:
            return result_response(ident, render(call_tool(name, params.get("arguments") or {})))
        except ToolError as exc:
            return result_response(ident, render_error(exc))
        except Exception as exc:                       # noqa: BLE001
            # A bug in this server is still a tool failure from the model's
            # point of view -- it should be able to try something else rather
            # than have the session die. The traceback goes to stderr, where a
            # human can find it and where it cannot corrupt the wire.
            traceback.print_exc(file=sys.stderr)
            return result_response(ident, render_error(exc))

    if is_notification:
        return None                       # unknown notifications are ignored
    return error_response(ident, METHOD_NOT_FOUND, f"unknown method {method!r}")


def result_response(ident, result):
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def error_response(ident, code, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# The stdio loop
# ---------------------------------------------------------------------------

def serve(stdin, stdout):
    state = {"ready": False, "client": {}}
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            # id is null because the message could not be parsed, so there is no
            # id to echo. That is exactly what the spec prescribes here.
            write(stdout, error_response(None, PARSE_ERROR, f"invalid JSON: {exc}"))
            continue

        # A batch is a JSON array. Answered because it is trivially cheap to
        # support and a client is permitted to send one; batches containing only
        # notifications get no response at all.
        if isinstance(message, list):
            replies = [r for r in (handle(m, state) for m in message) if r is not None]
            if replies:
                write(stdout, replies)
            continue

        response = handle(message, state)
        if response is not None:
            write(stdout, response)
    return 0


def write(stdout, payload):
    """One JSON object per line, flushed.

    ensure_ascii=False keeps UTF-8 as UTF-8 rather than \\uXXXX escapes -- an
    escaped character is six bytes instead of two, and the stream is UTF-8.

    The flush is not optional. Python buffers a pipe by default, so without it
    the client waits for a response that is sitting in a buffer, and the failure
    presents as a hang with no error anywhere.
    """
    stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def main():
    # Take the REAL stdout as the wire, then rebind sys.stdout to stderr so
    # anything that prints -- ours or a library's -- cannot corrupt the stream.
    # Cheaper than auditing every line for print() and it survives future edits.
    wire = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n",
                            write_through=True)
    reader = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="\n")
    sys.stdout = sys.stderr

    # Windows: the console default is cp1252, and a single non-ASCII byte in a
    # Prometheus error message raises UnicodeEncodeError INSIDE the error path,
    # turning a clean tool error into a crashed server. Session 2 hit exactly
    # this with a Unicode arrow in a log line.
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    try:
        return serve(reader, wire)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        # The client went away. Normal shutdown, not an error.
        return 0


if __name__ == "__main__":
    sys.exit(main())
