package com.rndworkbench.core.routing;

import java.util.Objects;

/**
 * Local-first routing for the 30-person pilot.
 *
 * <p>External models are disabled by the default policy. Enabling the future
 * extension is insufficient by itself: only PUBLIC data with explicit consent
 * may cross that boundary.</p>
 */
public final class PilotModelRoutingPolicy {
    private final boolean externalModelsEnabled;

    public PilotModelRoutingPolicy(boolean externalModelsEnabled) {
        this.externalModelsEnabled = externalModelsEnabled;
    }

    public static PilotModelRoutingPolicy pilotDefaults() {
        return new PilotModelRoutingPolicy(false);
    }

    public RoutingDecision route(RoutingRequest request) {
        Objects.requireNonNull(request, "request");
        return switch (request.preference()) {
            case AUTO -> autoRoute(request);
            case LOCAL -> localRoute(request);
            case CORPORATE -> corporateRoute(request);
            case EXTERNAL -> externalRoute(request);
        };
    }

    public boolean externalModelsEnabled() {
        return externalModelsEnabled;
    }

    private RoutingDecision autoRoute(RoutingRequest request) {
        if (request.availableRoutes().local()) {
            return RoutingDecision.selected(
                    ModelRoute.LOCAL,
                    RoutingReason.LOCAL_SELECTED,
                    false
            );
        }

        RoutingDecision corporate = corporateRoute(request);
        if (corporate.status() == RoutingStatus.SELECTED) {
            return corporate;
        }
        if (corporate.status() == RoutingStatus.BLOCKED) {
            return corporate;
        }
        return RoutingDecision.unavailable(RoutingReason.NO_PERMITTED_ROUTE);
    }

    private RoutingDecision localRoute(RoutingRequest request) {
        if (!request.availableRoutes().local()) {
            return RoutingDecision.unavailable(RoutingReason.LOCAL_UNAVAILABLE);
        }
        return RoutingDecision.selected(
                ModelRoute.LOCAL,
                RoutingReason.LOCAL_SELECTED,
                false
        );
    }

    private RoutingDecision corporateRoute(RoutingRequest request) {
        if (!request.classification().permitsCorporateRoute()) {
            return RoutingDecision.blocked(
                    RoutingReason.CLASSIFICATION_BLOCKS_CORPORATE
            );
        }
        if (!request.corporateScopeAuthorized()) {
            return RoutingDecision.blocked(
                    RoutingReason.CORPORATE_SCOPE_NOT_AUTHORIZED
            );
        }
        if (!request.availableRoutes().corporate()) {
            return RoutingDecision.unavailable(
                    RoutingReason.CORPORATE_UNAVAILABLE
            );
        }
        return RoutingDecision.selected(
                ModelRoute.CORPORATE,
                RoutingReason.CORPORATE_SELECTED,
                request.availableRoutes().local()
        );
    }

    private RoutingDecision externalRoute(RoutingRequest request) {
        if (!externalModelsEnabled) {
            return RoutingDecision.blocked(
                    RoutingReason.EXTERNAL_DISABLED_FOR_PILOT
            );
        }
        if (!request.classification().isExternalRouteEligible()) {
            return RoutingDecision.blocked(
                    RoutingReason.CLASSIFICATION_BLOCKS_EXTERNAL
            );
        }
        if (!request.explicitExternalConsent()) {
            return RoutingDecision.blocked(
                    RoutingReason.EXTERNAL_CONSENT_REQUIRED
            );
        }
        if (!request.availableRoutes().external()) {
            return RoutingDecision.unavailable(RoutingReason.EXTERNAL_UNAVAILABLE);
        }
        return RoutingDecision.selected(
                ModelRoute.EXTERNAL,
                RoutingReason.EXTERNAL_SELECTED,
                request.availableRoutes().local()
        );
    }
}
