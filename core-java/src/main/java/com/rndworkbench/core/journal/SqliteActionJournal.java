package com.rndworkbench.core.journal;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Clock;
import java.time.Instant;
import java.util.Objects;
import java.util.UUID;
import java.util.function.Supplier;

/**
 * SQLite-backed action journal. Each method is a complete transaction, so a
 * process restart cannot turn an existing claim into a second execution.
 *
 * <p>An abandoned {@code CLAIMED} row deliberately remains {@code IN_PROGRESS}.
 * Automatic lease expiry would permit duplicate external effects after a crash;
 * reconciliation must complete or explicitly repair the row instead.</p>
 */
public final class SqliteActionJournal implements ActionJournal {
    private static final String CLAIMED = "CLAIMED";
    private static final String COMPLETED = "COMPLETED";
    private static final int BUSY_TIMEOUT_MILLIS = 5_000;

    private final String jdbcUrl;
    private final Clock clock;
    private final Supplier<UUID> tokenSupplier;

    public SqliteActionJournal(Path databasePath) throws SQLException, IOException {
        this(databasePath, Clock.systemUTC(), UUID::randomUUID);
    }

    SqliteActionJournal(
            Path databasePath,
            Clock clock,
            Supplier<UUID> tokenSupplier
    ) throws SQLException, IOException {
        Objects.requireNonNull(databasePath, "databasePath");
        this.clock = Objects.requireNonNull(clock, "clock");
        this.tokenSupplier = Objects.requireNonNull(tokenSupplier, "tokenSupplier");
        Path absolute = databasePath.toAbsolutePath().normalize();
        Path parent = absolute.getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        jdbcUrl = "jdbc:sqlite:" + absolute;
        initialize();
    }

    @Override
    public ActionClaimResult claim(ActionClaimRequest request) throws SQLException {
        Objects.requireNonNull(request, "request");
        String claimToken = tokenSupplier.get().toString();
        Instant claimedAt = clock.instant();

        try (Connection connection = openConnection()) {
            connection.setAutoCommit(false);
            try {
                int inserted = insertClaim(
                        connection,
                        request,
                        claimToken,
                        claimedAt
                );
                StoredEntry entry = find(connection, request.idempotencyKey());
                connection.commit();

                if (inserted == 1) {
                    return ActionClaimResult.claimed(claimToken);
                }
                if (entry == null) {
                    throw new SQLException("Journal row disappeared during claim");
                }
                if (!entry.requestFingerprint().equals(request.requestFingerprint())) {
                    return ActionClaimResult.conflict();
                }
                if (CLAIMED.equals(entry.state())) {
                    return ActionClaimResult.inProgress();
                }
                if (COMPLETED.equals(entry.state()) && entry.result() != null) {
                    return ActionClaimResult.replay(entry.result());
                }
                throw new SQLException("Journal contains an invalid action state");
            } catch (SQLException | RuntimeException exception) {
                rollback(connection, exception);
                throw exception;
            }
        }
    }

    @Override
    public ActionCompletionResult complete(ActionCompletionRequest request)
            throws SQLException {
        Objects.requireNonNull(request, "request");
        try (Connection connection = openConnection()) {
            connection.setAutoCommit(false);
            try {
                int updated = recordCompletion(connection, request);
                if (updated == 1) {
                    connection.commit();
                    return ActionCompletionResult.recorded(request.result());
                }

                StoredEntry entry = find(connection, request.idempotencyKey());
                connection.commit();
                if (entry == null) {
                    return ActionCompletionResult.notClaimed();
                }
                if (!entry.requestFingerprint().equals(request.requestFingerprint())) {
                    return ActionCompletionResult.conflict();
                }
                if (COMPLETED.equals(entry.state())
                        && request.result().equals(entry.result())) {
                    return ActionCompletionResult.replay(entry.result());
                }
                return ActionCompletionResult.conflict();
            } catch (SQLException | RuntimeException exception) {
                rollback(connection, exception);
                throw exception;
            }
        }
    }

    private void initialize() throws SQLException {
        try (Connection connection = openConnection();
                Statement statement = connection.createStatement()) {
            statement.execute("PRAGMA journal_mode=WAL");
            statement.execute("""
                    CREATE TABLE IF NOT EXISTS action_journal (
                        idempotency_key TEXT PRIMARY KEY,
                        request_fingerprint TEXT NOT NULL,
                        correlation_id TEXT NOT NULL,
                        claim_token TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('CLAIMED', 'COMPLETED')),
                        claimed_at TEXT NOT NULL,
                        outcome TEXT,
                        result_code TEXT,
                        external_reference TEXT,
                        completed_at TEXT,
                        CHECK (
                            (state = 'CLAIMED'
                                AND outcome IS NULL
                                AND result_code IS NULL
                                AND external_reference IS NULL
                                AND completed_at IS NULL)
                            OR
                            (state = 'COMPLETED'
                                AND outcome IS NOT NULL
                                AND result_code IS NOT NULL
                                AND completed_at IS NOT NULL)
                        )
                    ) STRICT
                    """);
        }
    }

    private Connection openConnection() throws SQLException {
        Connection connection = DriverManager.getConnection(jdbcUrl);
        try (Statement statement = connection.createStatement()) {
            statement.execute("PRAGMA busy_timeout=" + BUSY_TIMEOUT_MILLIS);
            statement.execute("PRAGMA foreign_keys=ON");
        }
        return connection;
    }

    private static int insertClaim(
            Connection connection,
            ActionClaimRequest request,
            String claimToken,
            Instant claimedAt
    ) throws SQLException {
        try (PreparedStatement statement = connection.prepareStatement("""
                INSERT OR IGNORE INTO action_journal (
                    idempotency_key,
                    request_fingerprint,
                    correlation_id,
                    claim_token,
                    state,
                    claimed_at
                ) VALUES (?, ?, ?, ?, 'CLAIMED', ?)
                """)) {
            statement.setString(1, request.idempotencyKey());
            statement.setString(2, request.requestFingerprint());
            statement.setString(3, request.correlationId());
            statement.setString(4, claimToken);
            statement.setString(5, claimedAt.toString());
            return statement.executeUpdate();
        }
    }

    private static int recordCompletion(
            Connection connection,
            ActionCompletionRequest request
    ) throws SQLException {
        try (PreparedStatement statement = connection.prepareStatement("""
                UPDATE action_journal
                SET state = 'COMPLETED',
                    outcome = ?,
                    result_code = ?,
                    external_reference = ?,
                    completed_at = ?
                WHERE idempotency_key = ?
                    AND request_fingerprint = ?
                    AND claim_token = ?
                    AND state = 'CLAIMED'
                """)) {
            statement.setString(1, request.result().outcome().name());
            statement.setString(2, request.result().resultCode());
            statement.setString(3, request.result().externalReference());
            statement.setString(4, request.result().completedAt().toString());
            statement.setString(5, request.idempotencyKey());
            statement.setString(6, request.requestFingerprint());
            statement.setString(7, request.claimToken());
            return statement.executeUpdate();
        }
    }

    private static StoredEntry find(Connection connection, String idempotencyKey)
            throws SQLException {
        try (PreparedStatement statement = connection.prepareStatement("""
                SELECT request_fingerprint,
                       state,
                       outcome,
                       result_code,
                       external_reference,
                       completed_at
                FROM action_journal
                WHERE idempotency_key = ?
                """)) {
            statement.setString(1, idempotencyKey);
            try (ResultSet resultSet = statement.executeQuery()) {
                if (!resultSet.next()) {
                    return null;
                }
                String state = resultSet.getString("state");
                ActionExecutionResult result = null;
                if (COMPLETED.equals(state)) {
                    result = new ActionExecutionResult(
                            ActionOutcome.valueOf(resultSet.getString("outcome")),
                            resultSet.getString("result_code"),
                            resultSet.getString("external_reference"),
                            Instant.parse(resultSet.getString("completed_at"))
                    );
                }
                return new StoredEntry(
                        resultSet.getString("request_fingerprint"),
                        state,
                        result
                );
            }
        }
    }

    private static void rollback(Connection connection, Exception original) {
        try {
            connection.rollback();
        } catch (SQLException rollbackFailure) {
            original.addSuppressed(rollbackFailure);
        }
    }

    private record StoredEntry(
            String requestFingerprint,
            String state,
            ActionExecutionResult result
    ) {
    }
}
