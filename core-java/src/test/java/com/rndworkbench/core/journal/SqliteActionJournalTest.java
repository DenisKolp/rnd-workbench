package com.rndworkbench.core.journal;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class SqliteActionJournalTest {
    private static final String KEY = "jira:RND-42:version-7";
    private static final String FINGERPRINT = "a".repeat(64);
    private static final String OTHER_FINGERPRINT = "b".repeat(64);
    private static final Instant COMPLETED_AT =
            Instant.parse("2026-08-31T13:00:00Z");

    @TempDir
    Path temporaryDirectory;

    @Test
    void firstClaimOwnsTheActionAndDuplicateIsInProgress() throws Exception {
        Path database = temporaryDirectory.resolve("journal.sqlite");
        try (ActionJournal journal = new SqliteActionJournal(database)) {
            ActionClaimResult first = journal.claim(claim(FINGERPRINT));
            ActionClaimResult second = journal.claim(claim(FINGERPRINT));

            assertEquals(ActionClaimDisposition.CLAIMED, first.disposition());
            assertNotNull(first.claimToken());
            assertEquals(ActionClaimDisposition.IN_PROGRESS, second.disposition());
            assertNull(second.claimToken());
        }
    }

    @Test
    void crashAndRestartDoNotCreateASecondExecutionClaim() throws Exception {
        Path database = temporaryDirectory.resolve("restart-in-progress.sqlite");
        try (ActionJournal beforeCrash = new SqliteActionJournal(database)) {
            assertEquals(
                    ActionClaimDisposition.CLAIMED,
                    beforeCrash.claim(claim(FINGERPRINT)).disposition()
            );
        }

        try (ActionJournal afterRestart = new SqliteActionJournal(database)) {
            ActionInspectionResult inspection = afterRestart.inspect(
                    new ActionInspectionRequest(KEY, FINGERPRINT)
            );
            assertEquals(
                    ActionInspectionDisposition.IN_PROGRESS,
                    inspection.disposition()
            );
            assertNotNull(inspection.claimToken());
            assertEquals(
                    ActionClaimDisposition.IN_PROGRESS,
                    afterRestart.claim(claim(FINGERPRINT)).disposition()
            );
        }
    }

    @Test
    void inspectionDistinguishesMissingConflictAndCompletedWithoutMutation()
            throws Exception {
        Path database = temporaryDirectory.resolve("inspection.sqlite");
        try (ActionJournal journal = new SqliteActionJournal(database)) {
            assertEquals(
                    ActionInspectionDisposition.NOT_FOUND,
                    journal.inspect(
                            new ActionInspectionRequest(KEY, FINGERPRINT)
                    ).disposition()
            );
            ActionClaimResult claim = journal.claim(claim(FINGERPRINT));
            assertEquals(
                    ActionInspectionDisposition.CONFLICT,
                    journal.inspect(
                            new ActionInspectionRequest(KEY, OTHER_FINGERPRINT)
                    ).disposition()
            );
            journal.complete(completion(claim.claimToken(), FINGERPRINT, success()));
            ActionInspectionResult completed = journal.inspect(
                    new ActionInspectionRequest(KEY, FINGERPRINT)
            );
            assertEquals(
                    ActionInspectionDisposition.COMPLETED,
                    completed.disposition()
            );
            assertEquals(success(), completed.result());
        }
    }

    @Test
    void completedResultReplaysAfterRestart() throws Exception {
        Path database = temporaryDirectory.resolve("restart-completed.sqlite");
        ActionExecutionResult result = success();
        try (ActionJournal beforeRestart = new SqliteActionJournal(database)) {
            ActionClaimResult claim = beforeRestart.claim(claim(FINGERPRINT));
            ActionCompletionResult completion = beforeRestart.complete(
                    completion(claim.claimToken(), FINGERPRINT, result)
            );
            assertEquals(ActionCompletionDisposition.RECORDED, completion.disposition());
        }

        try (ActionJournal afterRestart = new SqliteActionJournal(database)) {
            ActionClaimResult replay = afterRestart.claim(claim(FINGERPRINT));
            assertEquals(ActionClaimDisposition.REPLAY, replay.disposition());
            assertEquals(result, replay.result());
        }
    }

    @Test
    void sameKeyWithDifferentFingerprintConflictsAfterRestart() throws Exception {
        Path database = temporaryDirectory.resolve("restart-conflict.sqlite");
        try (ActionJournal first = new SqliteActionJournal(database)) {
            first.claim(claim(FINGERPRINT));
        }

        try (ActionJournal second = new SqliteActionJournal(database)) {
            assertEquals(
                    ActionClaimDisposition.CONFLICT,
                    second.claim(claim(OTHER_FINGERPRINT)).disposition()
            );
        }
    }

    @Test
    void completionIsIdempotentButAChangedResultConflicts() throws Exception {
        Path database = temporaryDirectory.resolve("completion.sqlite");
        try (ActionJournal journal = new SqliteActionJournal(database)) {
            ActionClaimResult claim = journal.claim(claim(FINGERPRINT));
            ActionCompletionRequest completion = completion(
                    claim.claimToken(),
                    FINGERPRINT,
                    success()
            );

            assertEquals(
                    ActionCompletionDisposition.RECORDED,
                    journal.complete(completion).disposition()
            );
            assertEquals(
                    ActionCompletionDisposition.REPLAY,
                    journal.complete(completion).disposition()
            );

            ActionExecutionResult changed = new ActionExecutionResult(
                    ActionOutcome.FAILURE,
                    "CONNECTOR.TIMEOUT",
                    null,
                    COMPLETED_AT
            );
            assertEquals(
                    ActionCompletionDisposition.CONFLICT,
                    journal.complete(completion(
                            claim.claimToken(),
                            FINGERPRINT,
                            changed
                    )).disposition()
            );
        }
    }

    @Test
    void completionRequiresTheOwningTokenAndExistingClaim() throws Exception {
        Path database = temporaryDirectory.resolve("ownership.sqlite");
        try (ActionJournal journal = new SqliteActionJournal(database)) {
            assertEquals(
                    ActionCompletionDisposition.NOT_CLAIMED,
                    journal.complete(completion(
                            "87ea5d57-5405-4378-b7f9-90aa23d2db9d",
                            FINGERPRINT,
                            success()
                    )).disposition()
            );

            journal.claim(claim(FINGERPRINT));
            assertEquals(
                    ActionCompletionDisposition.CONFLICT,
                    journal.complete(completion(
                            "87ea5d57-5405-4378-b7f9-90aa23d2db9d",
                            FINGERPRINT,
                            success()
                    )).disposition()
            );
        }
    }

    @Test
    void concurrentClaimsHaveExactlyOneOwner() throws Exception {
        Path database = temporaryDirectory.resolve("concurrent.sqlite");
        int workers = 8;
        ExecutorService executor = Executors.newFixedThreadPool(workers);
        CountDownLatch ready = new CountDownLatch(workers);
        CountDownLatch start = new CountDownLatch(1);
        List<Future<ActionClaimDisposition>> futures = new ArrayList<>();
        try (ActionJournal journal = new SqliteActionJournal(database)) {
            for (int index = 0; index < workers; index++) {
                futures.add(executor.submit(() -> {
                    ready.countDown();
                    start.await();
                    return journal.claim(claim(FINGERPRINT)).disposition();
                }));
            }
            ready.await();
            start.countDown();
            List<ActionClaimDisposition> dispositions = new ArrayList<>();
            for (Future<ActionClaimDisposition> future : futures) {
                dispositions.add(future.get());
            }
            assertEquals(
                    1L,
                    dispositions.stream()
                            .filter(value -> value == ActionClaimDisposition.CLAIMED)
                            .count()
            );
            assertEquals(
                    workers - 1L,
                    dispositions.stream()
                            .filter(value -> value == ActionClaimDisposition.IN_PROGRESS)
                            .count()
            );
        } finally {
            executor.shutdownNow();
        }
    }

    @Test
    void databaseSchemaHasNoPayloadOrContentColumns() throws Exception {
        Path database = temporaryDirectory.resolve("minimal-schema.sqlite");
        try (ActionJournal journal = new SqliteActionJournal(database)) {
            assertNotNull(journal);
        }
        try (Connection connection = DriverManager.getConnection(
                        "jdbc:sqlite:" + database.toAbsolutePath()
                );
                Statement statement = connection.createStatement();
                ResultSet columns = statement.executeQuery(
                        "PRAGMA table_info(action_journal)"
                )) {
            Set<String> forbidden = Set.of(
                    "payload",
                    "content",
                    "prompt",
                    "response",
                    "secret",
                    "token_value"
            );
            while (columns.next()) {
                assertTrue(!forbidden.contains(columns.getString("name")));
            }
        }
    }

    private static ActionClaimRequest claim(String fingerprint) {
        return new ActionClaimRequest(KEY, fingerprint, "request-20260831-0001");
    }

    private static ActionCompletionRequest completion(
            String claimToken,
            String fingerprint,
            ActionExecutionResult result
    ) {
        return new ActionCompletionRequest(KEY, fingerprint, claimToken, result);
    }

    private static ActionExecutionResult success() {
        return new ActionExecutionResult(
                ActionOutcome.SUCCESS,
                "ISSUE.UPDATED",
                "RND-42",
                COMPLETED_AT
        );
    }
}
