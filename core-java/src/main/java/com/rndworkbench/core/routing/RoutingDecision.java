package com.rndworkbench.core.routing;

import java.util.Objects;
import java.util.Optional;

public record RoutingDecision(
        RoutingStatus status,
        ModelRoute route,
        RoutingReason reason,
        boolean localFallbackBeforeFirstOutput
) {
    public RoutingDecision {
        Objects.requireNonNull(status, "status");
        Objects.requireNonNull(reason, "reason");
        if ((status == RoutingStatus.SELECTED) != (route != null)) {
            throw new IllegalArgumentException(
                    "A route must be present only for a selected decision"
            );
        }
        if (localFallbackBeforeFirstOutput
                && (status != RoutingStatus.SELECTED || route == ModelRoute.LOCAL)) {
            throw new IllegalArgumentException(
                    "Local fallback applies only to a selected remote route"
            );
        }
    }

    public Optional<ModelRoute> selectedRoute() {
        return Optional.ofNullable(route);
    }

    public static RoutingDecision selected(
            ModelRoute route,
            RoutingReason reason,
            boolean localFallbackBeforeFirstOutput
    ) {
        return new RoutingDecision(
                RoutingStatus.SELECTED,
                route,
                reason,
                localFallbackBeforeFirstOutput
        );
    }

    public static RoutingDecision blocked(RoutingReason reason) {
        return new RoutingDecision(RoutingStatus.BLOCKED, null, reason, false);
    }

    public static RoutingDecision unavailable(RoutingReason reason) {
        return new RoutingDecision(RoutingStatus.UNAVAILABLE, null, reason, false);
    }
}
