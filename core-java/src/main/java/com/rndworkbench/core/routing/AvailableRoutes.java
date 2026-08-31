package com.rndworkbench.core.routing;

public record AvailableRoutes(
        boolean local,
        boolean corporate,
        boolean external
) {
    public boolean contains(ModelRoute route) {
        return switch (route) {
            case LOCAL -> local;
            case CORPORATE -> corporate;
            case EXTERNAL -> external;
        };
    }
}
