package com.rndworkbench.core.integration;

import java.util.Objects;

public record ActionOrigin(
        String actorId,
        OriginType originType,
        String requestId
) {
    public ActionOrigin {
        actorId = requireIdentifier(actorId, "actorId", 128);
        Objects.requireNonNull(originType, "originType");
        requestId = requireIdentifier(requestId, "requestId", 128);
    }

    private static String requireIdentifier(
            String value,
            String field,
            int maxLength
    ) {
        if (value == null || value.isBlank() || value.length() > maxLength) {
            throw new IllegalArgumentException(field + " must be 1.." + maxLength + " characters");
        }
        return value.strip();
    }
}
