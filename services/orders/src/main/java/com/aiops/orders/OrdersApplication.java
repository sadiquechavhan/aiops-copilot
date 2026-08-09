package com.aiops.orders;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

@SpringBootApplication
public class OrdersApplication {

    public static void main(String[] args) {
        SpringApplication.run(OrdersApplication.class, args);
    }

    /**
     * HTTP client for the inventory service. Read timeout is deliberately shorter
     * than the gateway's 5s, so that under injected latency the innermost call
     * fails first and the failure has an unambiguous origin in the trace.
     */
    @Bean
    RestClient inventoryClient(RestClient.Builder builder,
                               @Value("${downstream.url}") String downstreamUrl) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(2_000);
        factory.setReadTimeout(3_000);
        return builder.baseUrl(downstreamUrl).requestFactory(factory).build();
    }
}
