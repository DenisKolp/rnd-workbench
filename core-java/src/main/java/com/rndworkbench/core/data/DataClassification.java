package com.rndworkbench.core.data;

/**
 * Classification used before any model or integration boundary is selected.
 *
 * <p>The classification is deliberately independent from a vendor. Access
 * control is evaluated separately: a classification can permit a corporate
 * route while the current user or workspace still cannot.</p>
 */
public enum DataClassification {
    PUBLIC(true, true),
    PERSONAL(true, false),
    CORPORATE_INTERNAL(true, false),
    CONFIDENTIAL(true, false),
    RESTRICTED(false, false);

    private final boolean corporateRoutePermitted;
    private final boolean externalRouteEligible;

    DataClassification(
            boolean corporateRoutePermitted,
            boolean externalRouteEligible
    ) {
        this.corporateRoutePermitted = corporateRoutePermitted;
        this.externalRouteEligible = externalRouteEligible;
    }

    public boolean permitsCorporateRoute() {
        return corporateRoutePermitted;
    }

    /**
     * Eligibility is only one gate. External routing also requires a feature
     * opt-in and explicit consent for the request.
     */
    public boolean isExternalRouteEligible() {
        return externalRouteEligible;
    }
}
