package com.rndworkbench.core.journal;

import java.util.Objects;
import java.util.UUID;

public record ActionCompletionRequest(
        String idempotencyKey,
        String requestFingerprint,
        String claimToken,
        ActionExecutionResult result
) {
    public ActionCompletionRequest {
        // Reuse exactly the same boundary checks as a claim.
        new ActionClaimRequest(
                idempotencyKey,
                requestFingerprint,
                "completion-validation"
        );
        try {
            UUID.fromString(claimToken);
        } catch (NullPointerException | IllegalArgumentException exception) {
            throw new IllegalArgumentException("claimToken must be a UUID", exception);
        }
        Objects.requireNonNull(result, "result");
    }
}
