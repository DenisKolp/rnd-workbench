package com.rndworkbench.core.journal;

import java.sql.SQLException;

/** Durable idempotency boundary used before connector side effects. */
public interface ActionJournal extends AutoCloseable {
    ActionClaimResult claim(ActionClaimRequest request) throws SQLException;

    ActionCompletionResult complete(ActionCompletionRequest request) throws SQLException;

    @Override
    default void close() throws SQLException {
        // Implementations that open a connection per transaction need no close work.
    }
}
