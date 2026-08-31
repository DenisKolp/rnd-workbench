package com.rndworkbench.core.ipc.contract;

public record ActionInspectionPayload(
        String idempotencyKey,
        String requestFingerprint
) {
}
