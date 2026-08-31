package com.rndworkbench.core.journal;

import java.util.Objects;

public record ActionClaimResult(
        ActionClaimDisposition disposition,
        String claimToken,
        ActionExecutionResult result
) {
    public ActionClaimResult {
        Objects.requireNonNull(disposition, "disposition");
        boolean valid = switch (disposition) {
            case CLAIMED -> claimToken != null && result == null;
            case REPLAY -> claimToken == null && result != null;
            case IN_PROGRESS, CONFLICT -> claimToken == null && result == null;
        };
        if (!valid) {
            throw new IllegalArgumentException(
                    "claimToken/result do not match the claim disposition"
            );
        }
    }

    public static ActionClaimResult claimed(String claimToken) {
        return new ActionClaimResult(ActionClaimDisposition.CLAIMED, claimToken, null);
    }

    public static ActionClaimResult replay(ActionExecutionResult result) {
        return new ActionClaimResult(ActionClaimDisposition.REPLAY, null, result);
    }

    public static ActionClaimResult inProgress() {
        return new ActionClaimResult(ActionClaimDisposition.IN_PROGRESS, null, null);
    }

    public static ActionClaimResult conflict() {
        return new ActionClaimResult(ActionClaimDisposition.CONFLICT, null, null);
    }
}
