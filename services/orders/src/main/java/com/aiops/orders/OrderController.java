package com.aiops.orders;

import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.RestClient;

@RestController
public class OrderController {

    private static final Logger log = LoggerFactory.getLogger(OrderController.class);

    private final RestClient inventory;

    OrderController(RestClient inventory) {
        this.inventory = inventory;
    }

    public record OrderRequest(String sku, int qty) {}

    public record ReserveResponse(String sku, int reserved, int remaining) {}

    public record OrderResponse(String orderId, String sku, int qty, int remainingStock) {}

    @PostMapping(path = "/orders",
            consumes = MediaType.APPLICATION_JSON_VALUE,
            produces = MediaType.APPLICATION_JSON_VALUE)
    public OrderResponse create(@RequestBody OrderRequest request) {
        String orderId = UUID.randomUUID().toString();
        log.info("creating order {} sku={} qty={}", orderId, request.sku(), request.qty());

        ReserveResponse reservation = inventory.post()
                .uri("/reserve")
                .contentType(MediaType.APPLICATION_JSON)
                .body(request)
                .retrieve()
                .body(ReserveResponse.class);

        int remaining = reservation == null ? -1 : reservation.remaining();
        return new OrderResponse(orderId, request.sku(), request.qty(), remaining);
    }
}
