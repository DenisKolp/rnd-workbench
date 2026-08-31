package com.rndworkbench.core.journal;

public record ActionInspectionRequest(
        String idempotencyKey,
        String requestFingerprint
) {
    public ActionInspectionRequest {
        // Reuse the exact action identity boundary without creating a claim.
        new ActionClaimRequest(
                idempotencyKey,
                requestFingerprint,
                "inspection-validation"
        );
    }
}
