package com.rndworkbench.core.autonomy;

import java.util.Objects;

public record AutonomyDecision(
        AutonomyLevel level,
        boolean notificationRequired,
        boolean undoRequired,
        boolean previewRequired,
        boolean explicitConfirmationRequired,
        String reasonCode
) {
    public AutonomyDecision {
        Objects.requireNonNull(level, "level");
        if (reasonCode == null || reasonCode.isBlank()) {
            throw new IllegalArgumentException("reasonCode must not be blank");
        }
        if (undoRequired && !notificationRequired) {
            throw new IllegalArgumentException("Undo requires a visible notification");
        }
        if (level == AutonomyLevel.REQUIRE_PREVIEW && !previewRequired) {
            throw new IllegalArgumentException("Preview level must require preview");
        }
        if (level == AutonomyLevel.REQUIRE_EXPLICIT_CONFIRMATION
                && !explicitConfirmationRequired) {
            throw new IllegalArgumentException(
                    "Explicit-confirmation level must require confirmation"
            );
        }
    }
}
