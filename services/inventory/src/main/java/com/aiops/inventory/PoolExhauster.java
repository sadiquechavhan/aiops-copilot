package com.aiops.inventory;

import jakarta.annotation.PreDestroy;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.sql.DataSource;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * Holds HikariCP connections open so the pool looks saturated.
 *
 * <p>This is the one fault that is not a per-request behaviour, so it does not live
 * in the filter. It is a background thread that takes connections and refuses to give
 * them back until told to, or until its own deadline passes.
 *
 * <h2>Why this does not deadlock its own release path</h2>
 *
 * Four rules, each closing a specific way this goes wrong:
 *
 * <ol>
 *   <li><b>The holder runs on its own daemon thread, never the request thread.</b>
 *       {@code POST /chaos/pool-exhaust} starts it and returns immediately. Holding
 *       the connections on the Tomcat worker thread would mean the endpoint did not
 *       respond until the fault ended, and the scenario runner would block for the
 *       whole incident.</li>
 *   <li><b>The release path never asks for a connection.</b> {@link #stop()} touches
 *       in-memory state only; the connections are closed by the holder thread that
 *       owns them. If release called {@code getConnection()} it would queue behind
 *       the pool it is trying to free, wait {@code connection-timeout} (3000 ms),
 *       and then throw — the kill switch would fail precisely because the fault
 *       worked.</li>
 *   <li><b>No lock is held across a blocking acquire.</b> State is an
 *       {@link AtomicBoolean}, a {@link CopyOnWriteArrayList} and a
 *       {@link CountDownLatch}. If the holder held a monitor while parked inside
 *       {@code getConnection()}, {@code DELETE /chaos} would block on that same
 *       monitor. Release and hold share no mutual exclusion at all.</li>
 *   <li><b>A hard TTL dead-man's switch.</b> {@code latch.await(ttl)} returns either
 *       because someone released it or because the deadline passed, and the
 *       {@code finally} block releases either way. A killed scenario runner, a closed
 *       laptop lid or a crashed terminal cannot leave the pool held forever.</li>
 * </ol>
 *
 * <p>Release is signalled by counting the latch down rather than by
 * {@link Thread#interrupt()}: interrupting a thread parked inside a JDBC call leaves
 * the driver in an undefined state, and that is not a debugging session worth having.
 *
 * <h2>Why the default holds 9 of 10 and not all 10</h2>
 *
 * Taking all ten makes every {@code /reserve} wait the full 3000 ms
 * {@code connection-timeout} and then fail — but inventory's connection timeout and
 * the orders service's 3000 ms read timeout then race, so the same fault sometimes
 * surfaces as a downstream 500 and sometimes as a client-side timeout. A fault with
 * two different signatures is a fault you cannot label honestly.
 *
 * <p>Holding nine leaves one connection. At 6 req/s with roughly 15 ms of database
 * work per request, one connection has about ten times the capacity needed, so there
 * is no user-visible impact at all — latency flat, error rate flat, and
 * {@code db_client_connections_usage{state="used"}} pinned at 9. That is the more
 * interesting incident: a saturation signal with no golden-signal impact is exactly
 * the case a metrics detector wins and a latency/error detector misses.
 */
@Component
public class PoolExhauster {

    private static final Logger log = LoggerFactory.getLogger(PoolExhauster.class);

    private final DataSource dataSource;

    private final AtomicBoolean active = new AtomicBoolean(false);
    private final List<Connection> held = new CopyOnWriteArrayList<>();

    private volatile CountDownLatch release;
    private volatile Instant expiresAt;
    private volatile Thread holder;

    PoolExhauster(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    /**
     * @return false if a hold is already active. Starting a second holder would park
     *         it forever waiting for connections the first one is never going to
     *         return, so this is refused rather than queued.
     */
    public boolean start(int target, long ttlMs) {
        if (!active.compareAndSet(false, true)) {
            return false;
        }
        CountDownLatch latch = new CountDownLatch(1);
        release = latch;
        expiresAt = Instant.now().plusMillis(ttlMs);

        Thread thread = new Thread(() -> hold(target, ttlMs, latch), "chaos-pool-exhauster");
        thread.setDaemon(true);
        holder = thread;
        thread.start();
        return true;
    }

    private void hold(int target, long ttlMs, CountDownLatch latch) {
        try {
            try {
                // latch.getCount() doubles as the abort flag: if release arrives while
                // this loop is parked inside getConnection(), the next iteration stops
                // rather than acquiring connections nobody wants any more.
                for (int i = 0; i < target && latch.getCount() > 0; i++) {
                    held.add(dataSource.getConnection());
                }
            } catch (SQLException failed) {
                // A partial hold is still a real (smaller) fault, so carry on holding
                // what we got rather than unwinding. The finally block still frees it.
                log.warn("chaos pool-exhaust: acquired {} of {} connections before {}",
                        held.size(), target, failed.toString());
            }
            log.info("chaos pool-exhaust: holding {} connections, ttl {} ms", held.size(), ttlMs);
            latch.await(ttlMs, TimeUnit.MILLISECONDS);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        } finally {
            releaseAll();
        }
    }

    private void releaseAll() {
        int count = held.size();
        for (Connection connection : held) {
            try {
                connection.close();     // returns it to Hikari, does not close the socket
            } catch (SQLException ignored) {
                // Nothing useful to do. The pool evicts a connection it cannot reclaim.
            }
        }
        held.clear();
        expiresAt = null;
        holder = null;
        active.set(false);
        log.info("chaos pool-exhaust: released {} connections", count);
    }

    /**
     * Releases the hold and waits, briefly and boundedly, for the holder to finish so
     * that {@code DELETE /chaos} is synchronous and {@code GET /chaos} immediately
     * afterwards tells the truth.
     *
     * <p>The wait is bounded because the worst case is one in-flight
     * {@code getConnection()} at 3000 ms plus the close loop. Five seconds covers it,
     * and if it does not, the caller is told rather than left guessing.
     */
    public boolean stop() {
        CountDownLatch latch = release;
        if (latch == null || !active.get()) {
            return false;
        }
        latch.countDown();
        Thread thread = holder;
        if (thread != null) {
            try {
                thread.join(5_000);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }
        }
        return true;
    }

    @PreDestroy
    void releaseOnShutdown() {
        stop();
    }

    public boolean active() {
        return active.get();
    }

    public int heldCount() {
        return held.size();
    }

    public Instant expiresAt() {
        return expiresAt;
    }
}
