package com.aiops.inventory;

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
 *   POST   /chaos/pool-exhaust?hold=9&amp;ttl_ms=X   202   (inventory only)
 *   GET    /chaos                                 200
 *   DELETE /chaos                                 200
 * </pre>
 *
 * <p>No authentication, deliberately. These endpoints would be a catastrophe in a
 * real deployment; here the stack is bound to localhost and never leaves the machine,
 * and the production answer is the {@code CHAOS_ENABLED} switch defaulting to false
 * plus an admin port, not a password on a fault injector.
 *
 * <p>These control-plane calls log at INFO. They are not exported anywhere — the log
 * pipeline is off until Session 4 — but when it is turned on these lines become a
 * label the detector could read, and will need filtering at the collector.
 */
@RestController
@RequestMapping("/chaos")
public class ChaosController {

    private static final Logger log = LoggerFactory.getLogger(ChaosController.class);

    private static final String SERVICE = "inventory";

    /**
     * The endpoint refuses to create a fault it cannot label honestly.
     *
     * <p>The orders service reads from inventory with a 3000 ms timeout. Injecting
     * 3000 ms of latency here would not produce a latency incident, it would produce
     * a timeout-and-500 incident wearing a row in {@code ground_truth.jsonl} that says
     * "latency". 2000 ms leaves roughly a second of headroom over the measured ~90 ms
     * baseline.
     */
    private static final int MAX_LATENCY_MS = 2_000;

    private static final int POOL_MAX = 10;

    private final ChaosState state;
    private final PoolExhauster pool;

    ChaosController(ChaosState state, PoolExhauster pool) {
        this.state = state;
        this.pool = pool;
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
     * Returns 202, not 200: the hold is started on a background thread and this call
     * returns before all the connections have been taken. Saying "accepted" rather
     * than "done" is the honest status code, and the caller polls {@code GET /chaos}
     * if it needs to know the hold is fully established.
     */
    @PostMapping("/pool-exhaust")
    public ResponseEntity<Map<String, Object>> poolExhaust(
            @RequestParam(defaultValue = "9") int hold,
            @RequestParam(name = "ttl_ms", defaultValue = "600000") long ttlMs) {

        requireEnabled();
        if (hold < 1 || hold > POOL_MAX) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "hold must be 1.." + POOL_MAX);
        }
        if (ttlMs < 1_000 || ttlMs > 1_800_000) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "ttl_ms must be 1000..1800000");
        }
        if (!pool.start(hold, ttlMs)) {
            // Refused rather than queued: a second holder would park forever waiting
            // for connections the first one is never going to return.
            throw new ResponseStatusException(HttpStatus.CONFLICT, "a pool hold is already active");
        }
        log.info("chaos: pool-exhaust requested, target {} connections, ttl {} ms", hold, ttlMs);
        return ResponseEntity.accepted().body(status());
    }

    @GetMapping
    public Map<String, Object> status() {
        Map<String, Object> pools = new LinkedHashMap<>();
        pools.put("supported", true);
        pools.put("active", pool.active());
        pools.put("held", pool.heldCount());
        pools.put("expires_at", pool.expiresAt() == null ? null : pool.expiresAt().toString());

        Map<String, Object> counters = new LinkedHashMap<>();
        counters.put("requests_seen", state.requestsSeen());
        counters.put("requests_delayed", state.requestsDelayed());
        counters.put("requests_failed", state.requestsFailed());

        ChaosState.Faults faults = state.faults();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("service", SERVICE);
        body.put("enabled", state.enabled());
        body.put("active", !faults.inert() || pool.active());
        body.put("latency_ms", faults.latencyMs());
        body.put("jitter_ms", faults.jitterMs());
        body.put("error_rate_pct", faults.errorPct());
        body.put("pool_exhaust", pools);
        body.put("counters", counters);
        return body;
    }

    /**
     * Clears everything, including a pool hold. Synchronous by the time it returns —
     * see {@link PoolExhauster#stop()} — so a caller that gets 200 here can assert
     * against {@code GET /chaos} on the next line without a sleep.
     */
    @DeleteMapping
    public Map<String, Object> clear() {
        state.clear();
        boolean stoppedPool = pool.stop();
        log.info("chaos: cleared (pool hold released: {})", stoppedPool);
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
