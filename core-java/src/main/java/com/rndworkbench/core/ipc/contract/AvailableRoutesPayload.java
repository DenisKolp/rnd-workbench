package com.rndworkbench.core.ipc.contract;

public record AvailableRoutesPayload(
        Boolean local,
        Boolean corporate,
        Boolean external
) {
    public AvailableRoutesPayload {
        if (local == null || corporate == null || external == null) {
            throw new IllegalArgumentException("Every available route flag is required");
        }
    }
}
