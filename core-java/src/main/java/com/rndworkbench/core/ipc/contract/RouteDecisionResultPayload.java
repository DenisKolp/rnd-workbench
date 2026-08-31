package com.rndworkbench.core.ipc.contract;

import com.rndworkbench.core.routing.ModelRoute;
import com.rndworkbench.core.routing.RoutingDecision;
import com.rndworkbench.core.routing.RoutingReason;
import com.rndworkbench.core.routing.RoutingStatus;

import java.util.Objects;

public record RouteDecisionResultPayload(
        RoutingStatus status,
        ModelRoute route,
        RoutingReason reason,
        boolean localFallbackBeforeFirstOutput
) {
    public RouteDecisionResultPayload {
        Objects.requireNonNull(status, "status");
        Objects.requireNonNull(reason, "reason");
    }

    public static RouteDecisionResultPayload from(RoutingDecision decision) {
        Objects.requireNonNull(decision, "decision");
        return new RouteDecisionResultPayload(
                decision.status(),
                decision.route(),
                decision.reason(),
                decision.localFallbackBeforeFirstOutput()
        );
    }
}
