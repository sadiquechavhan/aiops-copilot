package com.aiops.inventory;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

@RestController
public class InventoryController {

    private static final Logger log = LoggerFactory.getLogger(InventoryController.class);

    private final JdbcTemplate jdbc;

    InventoryController(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public record ReserveRequest(String sku, int qty) {}

    public record ReserveResponse(String sku, int reserved, int remaining) {}

    @PostMapping(path = "/reserve",
            consumes = MediaType.APPLICATION_JSON_VALUE,
            produces = MediaType.APPLICATION_JSON_VALUE)
    public ReserveResponse reserve(@RequestBody ReserveRequest request) {

        // Conditional UPDATE rather than read-then-write: the "available >= ?" clause
        // makes check-and-decrement a single atomic statement, so concurrent
        // reservations cannot oversell without needing an explicit transaction.
        int updated = jdbc.update(
                "UPDATE inventory SET available = available - ? WHERE sku = ? AND available >= ?",
                request.qty(), request.sku(), request.qty());

        if (updated == 0) {
            // Conflates "unknown sku" with "insufficient stock". Acceptable for a
            // domain that exists only to produce a database span.
            log.warn("reservation rejected sku={} qty={}", request.sku(), request.qty());
            throw new ResponseStatusException(
                    HttpStatus.CONFLICT, "cannot reserve " + request.qty() + " of " + request.sku());
        }

        // Second statement on purpose: two database spans make the trace shape
        // obvious in Jaeger, and give Session 5 something to attribute latency to.
        Integer remaining = jdbc.queryForObject(
                "SELECT available FROM inventory WHERE sku = ?", Integer.class, request.sku());

        log.info("reserved sku={} qty={} remaining={}", request.sku(), request.qty(), remaining);
        return new ReserveResponse(request.sku(), request.qty(), remaining == null ? -1 : remaining);
    }
}
