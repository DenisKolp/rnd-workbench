package com.rndworkbench.core.ipc.contract;

import com.rndworkbench.core.journal.ActionOutcome;

import java.time.Instant;

public record ActionCompletionPayload(
        String idempotencyKey,
        String requestFingerprint,
        String claimToken,
        ActionOutcome outcome,
        String resultCode,
        String externalReference,
        Instant completedAt
) {
}
