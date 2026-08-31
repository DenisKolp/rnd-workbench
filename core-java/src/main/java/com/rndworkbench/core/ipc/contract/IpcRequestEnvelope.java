package com.rndworkbench.core.ipc.contract;

import com.fasterxml.jackson.databind.JsonNode;

import java.util.regex.Pattern;

/** Versioned request envelope for one JSONL frame. */
public record IpcRequestEnvelope(
        String version,
        String type,
        String correlationId,
        JsonNode payload
) {
    private static final Pattern TYPE =
            Pattern.compile("[a-z][a-z0-9]*(?:\\.[a-z][a-z0-9]*){1,3}");
    private static final Pattern CORRELATION =
            Pattern.compile("[A-Za-z0-9][A-Za-z0-9._:-]{0,127}");

    public IpcRequestEnvelope {
        if (version == null || version.isBlank() || version.length() > 16) {
            throw new IllegalArgumentException("version has an invalid format");
        }
        if (type == null || !TYPE.matcher(type).matches()) {
            throw new IllegalArgumentException("type has an invalid format");
        }
        if (correlationId == null || !CORRELATION.matcher(correlationId).matches()) {
            throw new IllegalArgumentException("correlationId has an invalid format");
        }
        if (payload == null || !payload.isObject()) {
            throw new IllegalArgumentException("payload must be a JSON object");
        }
    }
}
