package com.aiops.inventory;

import java.util.concurrent.atomic.LongAdder;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * The currently injected faults, plus counters proving what the filter actually did.
 *
 * <p><b>Why one volatile reference to an immutable record, and not three volatile
 * fields.</b> The filter reads this on every single request, so the read has to be
 * cheap and it has to be consistent. Three separate volatile fields would cost three
 * memory barriers and would allow a torn read — observing {@code latencyMs} from
 * before an update and {@code errorPct} from after it. One volatile read of an
 * immutable snapshot costs one barrier and cannot tear.
 *
 * <p>Writers are {@code synchronized}; readers are not. Injection happens a handful
 * of times per run, reads happen thousands of times, so all the contention is pushed
 * onto the rare side. This is copy-on-write.
 *
 * <p>The counters exist for a specific reason: Session 1's baseline has to stay
 * clean, because Session 4 learns "normal" from it. {@code requestsDelayed == 0}
 * after a baseline run is positive proof the mechanism was inert, which is a
 * stronger claim than "the latency numbers looked the same".
 */
@Component
public class ChaosState {

    /**
     * An immutable snapshot of every per-request fault. {@code jitterMs} is a
     * uniform +/- band around {@code latencyMs}; zero means a perfectly flat delay.
     */
    public record Faults(int latencyMs, int jitterMs, int errorPct) {

        public static final Faults NONE = new Faults(0, 0, 0);

        /** True when the filter has nothing to do and should get out of the way. */
        public boolean inert() {
            return latencyMs == 0 && errorPct == 0;
        }
    }

    private volatile Faults faults = Faults.NONE;

    // LongAdder rather than AtomicLong: striped, so concurrent increments from
    // Tomcat's worker threads do not contend on a single cache line.
    private final LongAdder requestsSeen = new LongAdder();
    private final LongAdder requestsDelayed = new LongAdder();
    private final LongAdder requestsFailed = new LongAdder();

    /**
     * When false the ChaosFilter is never registered, so the fault mechanism is not
     * merely idle, it is absent. Default true here because this stack exists to be
     * broken; in a real deployment the default would be false and this would be the
     * answer to "how do you stop fault injection reaching production".
     */
    private final boolean enabled;

    ChaosState(@Value("${chaos.enabled:true}") boolean enabled) {
        this.enabled = enabled;
    }

    public boolean enabled() {
        return enabled;
    }

    /** One volatile read. This is the whole hot path when nothing is injected. */
    public Faults faults() {
        return faults;
    }

    public synchronized void setLatency(int ms, int jitterMs) {
        Faults current = faults;
        faults = new Faults(ms, jitterMs, current.errorPct());
    }

    public synchronized void setErrorPct(int pct) {
        Faults current = faults;
        faults = new Faults(current.latencyMs(), current.jitterMs(), pct);
    }

    public synchronized void clear() {
        faults = Faults.NONE;
    }

    public void countSeen() {
        requestsSeen.increment();
    }

    public void countDelayed() {
        requestsDelayed.increment();
    }

    public void countFailed() {
        requestsFailed.increment();
    }

    public long requestsSeen() {
        return requestsSeen.sum();
    }

    public long requestsDelayed() {
        return requestsDelayed.sum();
    }

    public long requestsFailed() {
        return requestsFailed.sum();
    }
}
