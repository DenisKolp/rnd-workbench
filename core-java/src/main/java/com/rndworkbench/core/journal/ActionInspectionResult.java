package com.rndworkbench.core.journal;

import java.util.Objects;

/** Safe journal state used for restart reconciliation without re-execution. */
public record ActionInspectionResult(
        ActionInspectionDisposition disposition,
        String claimToken,
        ActionExecutionResult result
) {
    public ActionInspectionResult {
        Objects.requireNonNull(disposition, "disposition");
        boolean valid = switch (disposition) {
            case NOT_FOUND, CONFLICT -> claimToken == null && result == null;
            case IN_PROGRESS -> claimToken != null && result == null;
            case COMPLETED -> claimToken == null && result != null;
        };
        if (!valid) {
            throw new IllegalArgumentException(
                    "claimToken/result do not match the inspection disposition"
            );
        }
    }

    public static ActionInspectionResult notFound() {
        return new ActionInspectionResult(
                ActionInspectionDisposition.NOT_FOUND,
                null,
                null
        );
    }

    public static ActionInspectionResult inProgress(String claimToken) {
        return new ActionInspectionResult(
                ActionInspectionDisposition.IN_PROGRESS,
                claimToken,
                null
        );
    }

    public static ActionInspectionResult completed(ActionExecutionResult result) {
        return new ActionInspectionResult(
                ActionInspectionDisposition.COMPLETED,
                null,
                result
        );
    }

    public static ActionInspectionResult conflict() {
        return new ActionInspectionResult(
                ActionInspectionDisposition.CONFLICT,
                null,
                null
        );
    }
}
