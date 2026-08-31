package com.rndworkbench.core.ipc.contract;

import com.rndworkbench.core.data.DataClassification;
import com.rndworkbench.core.routing.RoutePreference;

import java.util.Objects;

public record RouteDecisionRequestPayload(
        DataClassification classification,
        RoutePreference preference,
        AvailableRoutesPayload availableRoutes,
        Boolean corporateScopeAuthorized,
        Boolean explicitExternalConsent
) {
    public RouteDecisionRequestPayload {
        Objects.requireNonNull(classification, "classification");
        Objects.requireNonNull(preference, "preference");
        Objects.requireNonNull(availableRoutes, "availableRoutes");
        if (corporateScopeAuthorized == null || explicitExternalConsent == null) {
            throw new IllegalArgumentException("Every routing gate is required");
        }
    }
}
