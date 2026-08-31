package com.rndworkbench.core.journal;

import java.util.regex.Pattern;

public record ActionClaimRequest(
        String idempotencyKey,
        String requestFingerprint,
        String correlationId
) {
    private static final Pattern KEY =
            Pattern.compile("[A-Za-z0-9][A-Za-z0-9._:-]{7,199}");
    private static final Pattern FINGERPRINT = Pattern.compile("[a-f0-9]{64}");
    private static final Pattern CORRELATION =
            Pattern.compile("[A-Za-z0-9][A-Za-z0-9._:-]{0,127}");

    public ActionClaimRequest {
        if (idempotencyKey == null || !KEY.matcher(idempotencyKey).matches()) {
            throw new IllegalArgumentException(
                    "idempotencyKey must be 8..200 safe ASCII characters"
            );
        }
        if (requestFingerprint == null
                || !FINGERPRINT.matcher(requestFingerprint).matches()) {
            throw new IllegalArgumentException(
                    "requestFingerprint must be a lowercase SHA-256 digest"
            );
        }
        if (correlationId == null || !CORRELATION.matcher(correlationId).matches()) {
            throw new IllegalArgumentException("correlationId has an invalid format");
        }
    }
}
