package com.rndworkbench.core.journal;

import java.time.Instant;
import java.util.Objects;
import java.util.regex.Pattern;

/**
 * Persisted connector result metadata. It intentionally cannot contain response
 * bodies, prompts, credentials, or arbitrary user content.
 */
public record ActionExecutionResult(
        ActionOutcome outcome,
        String resultCode,
        String externalReference,
        Instant completedAt
) {
    private static final Pattern RESULT_CODE =
            Pattern.compile("[A-Z0-9][A-Z0-9._:-]{0,127}");
    private static final Pattern EXTERNAL_REFERENCE =
            Pattern.compile("[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}");

    public ActionExecutionResult {
        Objects.requireNonNull(outcome, "outcome");
        if (resultCode == null || !RESULT_CODE.matcher(resultCode).matches()) {
            throw new IllegalArgumentException("resultCode has an invalid format");
        }
        if (externalReference != null
                && !EXTERNAL_REFERENCE.matcher(externalReference).matches()) {
            throw new IllegalArgumentException(
                    "externalReference has an invalid format"
            );
        }
        Objects.requireNonNull(completedAt, "completedAt");
    }
}
