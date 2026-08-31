package com.rndworkbench.core.routing;

import com.rndworkbench.core.data.DataClassification;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PilotModelRoutingPolicyTest {
    private final PilotModelRoutingPolicy policy =
            PilotModelRoutingPolicy.pilotDefaults();

    @Test
    void autoPrefersLocalForCorporateData() {
        RoutingDecision decision = policy.route(request(
                DataClassification.CORPORATE_INTERNAL,
                RoutePreference.AUTO,
                new AvailableRoutes(true, true, false),
                true,
                false
        ));

        assertEquals(RoutingStatus.SELECTED, decision.status());
        assertEquals(ModelRoute.LOCAL, decision.selectedRoute().orElseThrow());
        assertFalse(decision.localFallbackBeforeFirstOutput());
    }

    @Test
    void autoUsesAuthorizedCorporateRouteWhenLocalIsUnavailable() {
        RoutingDecision decision = policy.route(request(
                DataClassification.CONFIDENTIAL,
                RoutePreference.AUTO,
                new AvailableRoutes(false, true, false),
                true,
                false
        ));

        assertEquals(RoutingStatus.SELECTED, decision.status());
        assertEquals(ModelRoute.CORPORATE, decision.selectedRoute().orElseThrow());
    }

    @Test
    void restrictedDataNeverLeavesTheDevice() {
        RoutingDecision decision = policy.route(request(
                DataClassification.RESTRICTED,
                RoutePreference.CORPORATE,
                new AvailableRoutes(true, true, false),
                true,
                false
        ));

        assertEquals(RoutingStatus.BLOCKED, decision.status());
        assertEquals(RoutingReason.CLASSIFICATION_BLOCKS_CORPORATE, decision.reason());
    }

    @Test
    void corporateRouteRequiresCurrentScopeAuthorization() {
        RoutingDecision decision = policy.route(request(
                DataClassification.CORPORATE_INTERNAL,
                RoutePreference.CORPORATE,
                new AvailableRoutes(true, true, false),
                false,
                false
        ));

        assertEquals(RoutingStatus.BLOCKED, decision.status());
        assertEquals(RoutingReason.CORPORATE_SCOPE_NOT_AUTHORIZED, decision.reason());
    }

    @Test
    void externalRouteIsDisabledByPilotDefault() {
        RoutingDecision decision = policy.route(request(
                DataClassification.PUBLIC,
                RoutePreference.EXTERNAL,
                new AvailableRoutes(true, true, true),
                true,
                true
        ));

        assertEquals(RoutingStatus.BLOCKED, decision.status());
        assertEquals(RoutingReason.EXTERNAL_DISABLED_FOR_PILOT, decision.reason());
    }

    @Test
    void futureExternalOptInStillRequiresPublicDataAndConsent() {
        PilotModelRoutingPolicy optInPolicy = new PilotModelRoutingPolicy(true);

        RoutingDecision noConsent = optInPolicy.route(request(
                DataClassification.PUBLIC,
                RoutePreference.EXTERNAL,
                new AvailableRoutes(true, false, true),
                false,
                false
        ));
        RoutingDecision internalData = optInPolicy.route(request(
                DataClassification.CORPORATE_INTERNAL,
                RoutePreference.EXTERNAL,
                new AvailableRoutes(true, false, true),
                false,
                true
        ));
        RoutingDecision permitted = optInPolicy.route(request(
                DataClassification.PUBLIC,
                RoutePreference.EXTERNAL,
                new AvailableRoutes(true, false, true),
                false,
                true
        ));

        assertEquals(RoutingReason.EXTERNAL_CONSENT_REQUIRED, noConsent.reason());
        assertEquals(RoutingReason.CLASSIFICATION_BLOCKS_EXTERNAL, internalData.reason());
        assertEquals(ModelRoute.EXTERNAL, permitted.selectedRoute().orElseThrow());
        assertTrue(permitted.localFallbackBeforeFirstOutput());
    }

    private static RoutingRequest request(
            DataClassification classification,
            RoutePreference preference,
            AvailableRoutes routes,
            boolean corporateAuthorized,
            boolean externalConsent
    ) {
        return new RoutingRequest(
                classification,
                preference,
                routes,
                corporateAuthorized,
                externalConsent
        );
    }
}
