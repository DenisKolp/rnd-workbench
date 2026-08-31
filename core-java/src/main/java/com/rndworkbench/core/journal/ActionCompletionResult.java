package com.rndworkbench.core.journal;

import java.util.Objects;

public record ActionCompletionResult(
        ActionCompletionDisposition disposition,
        ActionExecutionResult result
) {
    public ActionCompletionResult {
        Objects.requireNonNull(disposition, "disposition");
        boolean terminal = disposition == ActionCompletionDisposition.RECORDED
                || disposition == ActionCompletionDisposition.REPLAY;
        if (terminal != (result != null)) {
            throw new IllegalArgumentException(
                    "result presence does not match completion disposition"
            );
        }
    }

    public static ActionCompletionResult recorded(ActionExecutionResult result) {
        return new ActionCompletionResult(
                ActionCompletionDisposition.RECORDED,
                result
        );
    }

    public static ActionCompletionResult replay(ActionExecutionResult result) {
        return new ActionCompletionResult(ActionCompletionDisposition.REPLAY, result);
    }

    public static ActionCompletionResult notClaimed() {
        return new ActionCompletionResult(
                ActionCompletionDisposition.NOT_CLAIMED,
                null
        );
    }

    public static ActionCompletionResult conflict() {
        return new ActionCompletionResult(ActionCompletionDisposition.CONFLICT, null);
    }
}
