package com.aiops.gateway;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

@SpringBootApplication
public class GatewayApplication {

    public static void main(String[] args) {
        SpringApplication.run(GatewayApplication.class, args);
    }

    /**
     * HTTP client for the orders service.
     *
     * <p>The timeouts are explicit on purpose. The JDK default is to wait forever,
     * which would make Session 2's injected latency indistinguishable from a hang
     * and would let a slow downstream pin every Tomcat worker thread.
     *
     * <p>Nothing here mentions OpenTelemetry. The Java agent rewrites the
     * underlying HTTP client at class-load time and injects the {@code traceparent}
     * header itself.
     */
    @Bean
    RestClient ordersClient(RestClient.Builder builder,
                            @Value("${downstream.url}") String downstreamUrl) {
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(2_000);
        factory.setReadTimeout(5_000);
        return builder.baseUrl(downstreamUrl).requestFactory(factory).build();
    }
}
