package com.aiops.gateway;

import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Hand-rolled liveness endpoint, used by the Compose healthcheck.
 *
 * <p>Production would use spring-boot-starter-actuator, which also gives readiness,
 * dependency health and metrics. That is a dependency this session did not authorise,
 * and three lines here buy the container ordering we actually need.
 */
@RestController
public class HealthController {

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "UP", "service", "gateway");
    }
}
