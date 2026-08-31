package com.rndworkbench.core.ipc.contract;

import java.util.Objects;

public record IpcError(String code, String message) {
    public IpcError {
        Objects.requireNonNull(code, "code");
        Objects.requireNonNull(message, "message");
    }
}
