package com.rndworkbench.core.ipc.contract;

import com.rndworkbench.core.autonomy.ActionKind;

import java.util.Objects;

/** Content-free action category used by the pilot autonomy policy. */
public record AutonomyDecisionRequestPayload(ActionKind actionKind) {
    public AutonomyDecisionRequestPayload {
        Objects.requireNonNull(actionKind, "actionKind");
    }
}
