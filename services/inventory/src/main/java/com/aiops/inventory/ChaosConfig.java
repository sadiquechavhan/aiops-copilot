package com.aiops.inventory;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;

/**
 * Registers the chaos filter, and only if chaos is switched on.
 *
 * <p>Registered explicitly rather than by annotating {@link ChaosFilter} with
 * {@code @Component}, for two reasons: auto-registration gives no control over
 * ordering, and it offers no way to leave the filter out of the chain entirely.
 * With {@code CHAOS_ENABLED=false} the filter is not merely idle, it is absent —
 * which is a materially stronger statement to be able to make about a fault
 * injector.
 *
 * <p>{@code CHAOS_ENABLED} binds to {@code chaos.enabled} through Spring's relaxed
 * environment binding, so no change to {@code application.yml} is needed.
 *
 * <p>Order is {@code HIGHEST_PRECEDENCE + 10}: early enough that the fault applies
 * before any application filter has done work, late enough to sit after Spring
 * Boot's own character-encoding filter. It does not affect whether the injected
 * delay lands inside the OTel span — the agent opens that span at the first filter
 * in the chain regardless — it only keeps the fault close to "the request arrived".
 */
@Configuration
class ChaosConfig {

    @Bean
    @ConditionalOnProperty(name = "chaos.enabled", havingValue = "true", matchIfMissing = true)
    FilterRegistrationBean<ChaosFilter> chaosFilterRegistration(ChaosState state) {
        FilterRegistrationBean<ChaosFilter> registration =
                new FilterRegistrationBean<>(new ChaosFilter(state));
        registration.addUrlPatterns("/*");
        registration.setName("chaosFilter");
        registration.setOrder(Ordered.HIGHEST_PRECEDENCE + 10);
        return registration;
    }
}
