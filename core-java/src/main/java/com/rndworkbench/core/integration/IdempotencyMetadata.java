package com.rndworkbench.core.integration;

import java.time.Instant;
import java.util.Objects;
import java.util.regex.Pattern;

public record IdempotencyMetadata(
        String key,
        String requestFingerprint,
        String correlationId,
        Instant issuedAt,
        int attempt
) {
    private static final Pattern KEY = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._:-]{7,199}");
    private static final Pattern SHA_256 = Pattern.compile("[a-f0-9]{64}");

    public IdempotencyMetadata {
        if (key == null || !KEY.matcher(key).matches()) {
            throw new IllegalArgumentException(
                    "Idempotency key must be 8..200 safe ASCII characters"
            );
        }
        if (requestFingerprint == null
                || !SHA_256.matcher(requestFingerprint).matches()) {
            throw new IllegalArgumentException(
                    "requestFingerprint must be a lowercase SHA-256 digest"
            );
        }
        if (correlationId == null
                || correlationId.isBlank()
                || correlationId.length() > 128) {
            throw new IllegalArgumentException(
                    "correlationId must be 1..128 characters"
            );
        }
        correlationId = correlationId.strip();
        Objects.requireNonNull(issuedAt, "issuedAt");
        if (attempt < 1) {
            throw new IllegalArgumentException("attempt must be at least 1");
        }
    }

    /** A retry keeps the operation identity and only increments its attempt. */
    public IdempotencyMetadata nextAttempt() {
        if (attempt == Integer.MAX_VALUE) {
            throw new IllegalStateException("attempt counter exhausted");
        }
        return new IdempotencyMetadata(
                key,
                requestFingerprint,
                correlationId,
                issuedAt,
                attempt + 1
        );
    }
}
