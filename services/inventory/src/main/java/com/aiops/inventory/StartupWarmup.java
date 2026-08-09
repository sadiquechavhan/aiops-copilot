package com.aiops.inventory;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * Issues one trivial query during startup, before the container reports healthy.
 *
 * <p>Without this, the first real request pays for HikariCP pool creation, the
 * Postgres TCP handshake, JDBC driver class loading, and the OTel agent's
 * bytecode instrumentation of all of the above. Measured on a fully cold chain,
 * that exceeded the orders service's 3s read timeout and returned a 500.
 *
 * <p>This matters beyond tidiness. Session 2 measures detection accuracy against
 * deliberately injected faults, so a spurious 500 on every {@code docker compose
 * up} would be a false positive baked into the ground truth before a single
 * fault is injected.
 */
@Component
class StartupWarmup implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(StartupWarmup.class);

    private final JdbcTemplate jdbc;

    StartupWarmup(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public void run(ApplicationArguments args) {
        long startedNanos = System.nanoTime();
        Integer skuCount = jdbc.queryForObject("SELECT count(*) FROM inventory", Integer.class);
        log.info("warm-up query completed in {} ms, {} SKUs present",
                (System.nanoTime() - startedNanos) / 1_000_000, skuCount);
    }
}
