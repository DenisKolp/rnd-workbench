package com.rndworkbench.core.routing;

import com.rndworkbench.core.data.DataClassification;

import java.util.Objects;

public record RoutingRequest(
        DataClassification classification,
        RoutePreference preference,
        AvailableRoutes availableRoutes,
        boolean corporateScopeAuthorized,
        boolean explicitExternalConsent
) {
    public RoutingRequest {
        Objects.requireNonNull(classification, "classification");
        Objects.requireNonNull(preference, "preference");
        Objects.requireNonNull(availableRoutes, "availableRoutes");
    }
}
