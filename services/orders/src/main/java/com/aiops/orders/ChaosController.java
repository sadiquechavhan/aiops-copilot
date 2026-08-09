package com.aiops.orders;

import java.util.LinkedHashMap;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

/**
 * The chaos control plane. Same shape in all three services.
 *
 * <pre>
 *   POST   /chaos/latency?ms=N[&amp;jitter=J]        200
 *   POST   /chaos/error-rate?pct=N                200
 *   POST   /chaos/pool-exhaust                    501  (inventory only; no pool here)
 *   GET    /chaos                                 200
 *   DELETE /chaos                                 200
 * </pre>
 *
 * <p>This differs from inventory's copy in exactly three places: the service name,
 * the latency ceiling, and the pool-exhaust branch. {@code ChaosState},
 * {@code ChaosFilter} and {@code ChaosConfig} are byte-identical across all three
 * services apart from their package line.
 *
 * <p>The duplication is deliberate. A shared {@code chaos-lib} Maven module would
 * remove it, and would contradict the decision to keep three independently deployable
 * services with no shared parent pom. At this size the copies are cheaper than the
 * build coupling; the risk that they drift is real and is recorded rather than
 * assumed away.
 *
 * <p>No authentication, deliberately. These endpoints would be a catastrophe in a
 * real deployment; here the stack is bound to localhost and never leaves the machine,
 * and the production answer is the {@code CHAOS_ENABLED} switch defaulting to false
 * plus an admin port, not a password on a fault injector.
 */
@RestController
@RequestMapping("/chaos")
public class ChaosController {

    private static final Logger log = LoggerFactory.getLogger(ChaosController.class);

    private static final String SERVICE = "orders";

    /**
     * The endpoint refuses to create a fault it cannot label honestly.
     *
     * <p>The gateway reads from orders with a 5000 ms timeout. Injecting anything
     * close to that would not produce a latency incident, it would produce a
     * timeout-and-500 incident wearing a row in {@code ground_truth.jsonl} that says
     * "latency". 2000 ms leaves ample headroom over the measured ~170 ms baseline.
     */
    private static final int MAX_LATENCY_MS = 2_000;

    private final ChaosState state;

    ChaosController(ChaosState state) {
        this.state = state;
    }

    @PostMapping("/latency")
    public Map<String, Object> latency(@RequestParam int ms,
                                       @RequestParam(defaultValue = "0") int jitter) {
        requireEnabled();
        if (ms < 0 || ms > MAX_LATENCY_MS) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST,
                    "ms must be 0.." + MAX_LATENCY_MS + " for " + SERVICE
                            + "; higher would breach a caller's read timeout and "
                            + "turn this into an error fault");
        }
        if (jitter < 0 || jitter > ms) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "jitter must be 0..ms");
        }
        state.setLatency(ms, jitter);
        log.info("chaos: latency set to {} ms (jitter {} ms)", ms, jitter);
        return status();
    }

    @PostMapping("/error-rate")
    public Map<String, Object> errorRate(@RequestParam int pct) {
        requireEnabled();
        if (pct < 0 || pct > 100) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "pct must be 0..100");
        }
        state.setErrorPct(pct);
        log.info("chaos: error rate set to {}%", pct);
        return status();
    }

    /**
     * 501, not 404. This service has no connection pool to exhaust, and "the route
     * exists but this instance cannot do it" is a different fact from "no such route"
     * — the scenario runner distinguishes a service that is missing the feature from
     * a service that is missing the whole chaos mechanism.
     */
    @PostMapping("/pool-exhaust")
    public ResponseEntity<Map<String, Object>> poolExhaust() {
        throw new ResponseStatusException(HttpStatus.NOT_IMPLEMENTED,
                SERVICE + " has no database connection pool; pool-exhaust is inventory only");
    }

    @GetMapping
    public Map<String, Object> status() {
        Map<String, Object> pools = new LinkedHashMap<>();
        pools.put("supported", false);
        pools.put("active", false);
        pools.put("held", 0);
        pools.put("expires_at", null);

        Map<String, Object> counters = new LinkedHashMap<>();
        counters.put("requests_seen", state.requestsSeen());
        counters.put("requests_delayed", state.requestsDelayed());
        counters.put("requests_failed", state.requestsFailed());

        ChaosState.Faults faults = state.faults();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("service", SERVICE);
        body.put("enabled", state.enabled());
        body.put("active", !faults.inert());
        body.put("latency_ms", faults.latencyMs());
        body.put("jitter_ms", faults.jitterMs());
        body.put("error_rate_pct", faults.errorPct());
        body.put("pool_exhaust", pools);
        body.put("counters", counters);
        return body;
    }

    @DeleteMapping
    public Map<String, Object> clear() {
        state.clear();
        log.info("chaos: cleared");
        return status();
    }

    private void requireEnabled() {
        if (!state.enabled()) {
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE,
                    "chaos is disabled on this instance (CHAOS_ENABLED=false); the filter "
                            + "is not registered, so injecting would be a lie");
        }
    }
}
