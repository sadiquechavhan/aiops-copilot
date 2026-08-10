"""
The error taxonomy and two statistics helpers.

This module imports nothing else from the package, so it can be imported from
anywhere without a cycle.

WHY THE ERROR TYPES ARE A CLOSED SET
------------------------------------
The requirement is that "no data" and "broken" must never look the same. That is
not achievable with a single error string, because the caller is a language
model: it pattern-matches on shape, and two failures that render similarly will
be treated similarly.

So every failure carries a `kind` drawn from a closed set of four, and each one
implies a different next action for the caller:

    bad_argument         -- the caller's fault. Fix the argument and retry.
    backend_unreachable  -- nothing is listening. Retrying is pointless until
                            somebody starts the stack.
    backend_error        -- the backend answered, and said no. The query itself
                            is wrong; retrying it unchanged will fail again.
    timeout              -- it is there but did not answer in time. Retrying a
                            NARROWER question may work.

"The series does not exist" is deliberately NOT in this list. It is not an error
at all -- see metrics.py, which returns status="empty" with a reason. Session 2
has a metric (the 5xx series) that legitimately does not exist until the first
500 occurs, and reporting that as a failure would be a lie in the direction that
makes a healthy system look broken.
"""

import math


class ToolError(Exception):
    """Base for everything a tool reports back through the `error` channel.

    Note this is an ordinary MCP *result* with isError=true, not a JSON-RPC
    protocol error. The distinction matters: a JSON-RPC error is delivered to
    the client, and the model never sees it. A tool error is delivered to the
    model, which is the only party that can decide to ask a different question.
    """

    kind = "internal"

    def __init__(self, detail, **extra):
        super().__init__(detail)
        self.detail = detail
        self.extra = extra

    def payload(self):
        body = {"kind": self.kind, "detail": self.detail}
        body.update(self.extra)
        return body


class BadArgument(ToolError):
    kind = "bad_argument"


class BackendUnreachable(ToolError):
    kind = "backend_unreachable"


class BackendError(ToolError):
    kind = "backend_error"


class BackendTimeout(ToolError):
    kind = "timeout"


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def percentile(values, p):
    """Linear-interpolation percentile -- the same definition numpy uses.

    Named rather than hand-waved because "p95" is ambiguous: nearest-rank and
    interpolated disagree on small samples, and query_traces computes p95 over
    as few as 20 traces. Being able to say which one this is matters more than
    which one it is.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (p / 100.0)
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] * (high - rank) + ordered[high] * (rank - low)


def rnd(value, digits=1):
    """Round for the wire, preserving None.

    Not cosmetic. 0.4884736 serialises as nine characters and about nine tokens;
    488.5 is five characters and one. Across a dozen series and a dozen points
    each that is the difference between a cheap response and an expensive one,
    and no question this tool answers needs the seventh decimal place.
    """
    if value is None:
        return None
    if value != value:              # NaN -- histogram_quantile emits it freely
        return None
    return round(value, digits)


def median(values):
    return percentile(values, 50)
