package com.rndworkbench.core.integration;

public enum IntegrationIntent {
    READ,
    DRAFT,
    WRITE,
    PUBLISH,
    DELETE,
    PERMISSIONS,
    MASS;

    public boolean isMutation() {
        return switch (this) {
            case READ, DRAFT -> false;
            case WRITE, PUBLISH, DELETE, PERMISSIONS, MASS -> true;
        };
    }
}
