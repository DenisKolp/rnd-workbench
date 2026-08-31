package com.rndworkbench.core.ipc.contract;

public record ActionClaimPayload(
        String idempotencyKey,
        String requestFingerprint
) {
}
