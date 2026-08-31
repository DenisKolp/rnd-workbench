package com.rndworkbench.core.ipc.contract;

import java.util.Objects;

/** Deterministic response envelope; exactly one of payload and error is present. */
public record IpcResponseEnvelope(
        String version,
        String type,
        String correlationId,
        boolean ok,
        Object payload,
        IpcError error
) {
    public IpcResponseEnvelope {
        Objects.requireNonNull(version, "version");
        Objects.requireNonNull(type, "type");
        Objects.requireNonNull(correlationId, "correlationId");
        if (ok == (error != null) || ok != (payload != null)) {
            throw new IllegalArgumentException(
                    "Successful responses have payload; failed responses have error"
            );
        }
    }

    public static IpcResponseEnvelope success(
            String version,
            String type,
            String correlationId,
            Object payload
    ) {
        return new IpcResponseEnvelope(
                version,
                type,
                correlationId,
                true,
                payload,
                null
        );
    }

    public static IpcResponseEnvelope failure(
            String version,
            String correlationId,
            String code,
            String message
    ) {
        return new IpcResponseEnvelope(
                version,
                "error",
                correlationId,
                false,
                null,
                new IpcError(code, message)
        );
    }
}
