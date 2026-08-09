package com.aiops.inventory;

import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Liveness only — it deliberately does not touch the database. A healthcheck that
 * queries Postgres would make Compose restart this container during a database
 * incident, which is exactly the wrong behaviour for the faults we inject later.
 */
@RestController
public class HealthController {

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "UP", "service", "inventory");
    }
}
