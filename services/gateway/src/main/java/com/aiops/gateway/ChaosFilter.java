package com.aiops.gateway;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.time.Instant;
import java.util.concurrent.ThreadLocalRandom;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * Injects per-request faults: added latency, and a percentage of 500s.
 *
 * <p><b>Why a servlet Filter and not a HandlerInterceptor or AOP advice.</b> The
 * contract is "add N ms to every subsequent request", which is a statement about
 * HTTP requests. The filter is the servlet-spec seam where "a request" is the unit;
 * a HandlerInterceptor only ever sees requests that survived Spring MVC handler
 * mapping, and {@code @Around} advice intercepts a Java method call, which is the
 * wrong thing entirely and would have needed a new AspectJ dependency.
 *
 * <p><b>Why the sleep lands inside the telemetry.</b> The OpenTelemetry agent starts
 * its SERVER span at the outermost filter in the chain and derives
 * {@code http.server.request.duration} from that span. Sleeping inside this filter
 * body is therefore inside both the span and the metric. A delay injected outside the
 * container — at the connector, or with a network shim — would be real to the client
 * and invisible to Grafana, which is the exact failure mode this session is meant to
 * avoid.
 *
 * <p><b>Why the delay goes before {@code chain.doFilter} and not after.</b> It then
 * appears in Jaeger as a gap between this service's SERVER span starting and its
 * first child span. That gap is the visual signature of "this service is slow",
 * as distinct from "its downstream is slow", and it is what makes an injected
 * gateway delay distinguishable from an injected inventory delay in a trace.
 *
 * <p><b>Why /chaos and /health are exempt.</b> If {@code error-rate=100} could fail
 * {@code DELETE /chaos} there would be no way to turn the fault off — the kill switch
 * must not be able to disable itself. {@code /health} is exempt because the Compose
 * healthcheck has a 3s timeout: an injected delay would flip the container to
 * unhealthy, adding a second, unlabelled failure mode to a run whose whole purpose is
 * to have exactly one labelled fault at a time. That is a deliberate loss of realism
 * — a real latency fault would slow the health endpoint too — recorded rather than
 * hidden.
 */
public class ChaosFilter extends OncePerRequestFilter {

    /**
     * Marks an injected 500 for the scenario runner and for nothing else.
     *
     * <p>This is the whole answer to "should injected errors be distinguishable from
     * real ones". In telemetry they are not: same status code, same
     * {@code http_response_status_code="500"} series, same ERROR span status. If the
     * marker were a span attribute, Session 4's detector could score 100% by reading
     * it and would have measured nothing. The OTel agent does not capture response
     * headers unless {@code otel.instrumentation.http.server.capture-response-headers}
     * is set, and it is not — so this header reaches the injector, which is an HTTP
     * client, and reaches no telemetry backend.
     */
    static final String MARKER_HEADER = "X-Chaos-Injected";

    private final ChaosState state;

    ChaosFilter(ChaosState state) {
        this.state = state;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain chain) throws ServletException, IOException {

        state.countSeen();

        ChaosState.Faults faults = state.faults();      // the one volatile read
        if (faults.inert()) {
            chain.doFilter(request, response);
            return;
        }

        int delayMs = faults.latencyMs();
        if (delayMs > 0) {
            if (faults.jitterMs() > 0) {
                delayMs += ThreadLocalRandom.current()
                        .nextInt(-faults.jitterMs(), faults.jitterMs() + 1);
                delayMs = Math.max(delayMs, 0);
            }
            state.countDelayed();
            try {
                Thread.sleep(delayMs);
            } catch (InterruptedException interrupted) {
                // Restore the flag and carry on. Swallowing the interrupt silently
                // would leave the thread unable to respond to shutdown.
                Thread.currentThread().interrupt();
            }
        }

        if (faults.errorPct() > 0
                && ThreadLocalRandom.current().nextInt(100) < faults.errorPct()) {
            state.countFailed();
            writeInjectedError(response, request.getRequestURI());
            return;                                     // fail fast: the chain never runs
        }

        chain.doFilter(request, response);
    }

    /**
     * Never fault the control plane or the container healthcheck. See the class
     * comment — this is a correctness requirement, not a convenience.
     */
    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        String path = request.getRequestURI();
        return path.startsWith("/chaos") || path.equals("/health");
    }

    /**
     * Writes a 500 that looks like Spring's own.
     *
     * <p>Deliberately {@code setStatus} plus a body rather than {@code sendError}.
     * {@code sendError} triggers Tomcat's error-page dispatch to {@code /error}, which
     * risks the agent relabelling {@code http.route} and would make injected 500s land
     * on a different metric series from real ones — a leak, and a broken dashboard.
     *
     * <p>Known limitation, measured against a real 500 in Jaeger rather than assumed.
     * An injected 500 and a genuine one are identical on {@code http.route},
     * {@code http.response.status_code}, {@code error.type} and span status — so they
     * land on the same Prometheus series with the same labels. That is where the
     * cheating risk lived, and it is closed.
     *
     * <p>Two differences remain, both in the trace and neither in the metric. The
     * injected span carries no {@code exception} event, where a 500 raised by a thrown
     * exception carries one complete with a stacktrace; and it has no child spans,
     * because the chain never ran. A trace-shape-aware detector could key on either.
     * Closing the gap would mean throwing a named {@code ChaosInjectedException} after
     * handler mapping, which trades an implicit signature for a louder explicit one.
     *
     * <p>Note the first tell is not absolute: a genuine 500 returned deliberately by a
     * controller, rather than thrown, would also carry no exception event.
     */
    private void writeInjectedError(HttpServletResponse response, String path) throws IOException {
        response.setStatus(HttpServletResponse.SC_INTERNAL_SERVER_ERROR);
        response.setContentType("application/json");
        response.setCharacterEncoding("UTF-8");
        response.setHeader(MARKER_HEADER, "error-rate");
        response.getWriter().write("{\"timestamp\":\"" + Instant.now()
                + "\",\"status\":500,\"error\":\"Internal Server Error\",\"path\":\""
                + path + "\"}");
    }
}
