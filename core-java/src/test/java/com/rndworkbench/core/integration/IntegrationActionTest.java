package com.rndworkbench.core.integration;

import com.rndworkbench.core.autonomy.ActionKind;
import com.rndworkbench.core.data.DataClassification;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class IntegrationActionTest {
    private static final Instant NOW = Instant.parse("2026-08-31T12:00:00Z");

    @Test
    void actionCopiesPayloadAndCreatesStableFingerprint() {
        List<Object> labels = new ArrayList<>(List.of("voice", "pilot"));
        Map<String, Object> parameters = new LinkedHashMap<>();
        parameters.put("summary", "Проверить качество TTS");
        parameters.put("labels", labels);

        IntegrationAction action = action(parameters);
        String first = action.idempotency().requestFingerprint();
        String second = IntegrationActionContract.fingerprint(
                action.connector(),
                action.operation(),
                action.intent(),
                action.actionKind(),
                action.classification(),
                Map.of(
                        "labels", List.of("voice", "pilot"),
                        "summary", "Проверить качество TTS"
                )
        );

        labels.add("changed-after-create");
        assertEquals(first, second);
        assertEquals(List.of("voice", "pilot"), action.parameters().get("labels"));
        assertThrows(
                UnsupportedOperationException.class,
                () -> action.parameters().put("other", true)
        );
        @SuppressWarnings("unchecked")
        List<Object> frozenLabels = (List<Object>) action.parameters().get("labels");
        assertThrows(
                UnsupportedOperationException.class,
                () -> frozenLabels.add("other")
        );
    }

    @Test
    void retryPreservesIdentityAndIncrementsAttempt() {
        IntegrationAction original = action(Map.of("issue", "RND-42"));
        IntegrationAction retry = original.nextAttempt();

        assertEquals(original.actionId(), retry.actionId());
        assertEquals(original.idempotency().key(), retry.idempotency().key());
        assertEquals(
                original.idempotency().requestFingerprint(),
                retry.idempotency().requestFingerprint()
        );
        assertEquals(2, retry.idempotency().attempt());
        assertTrue(retry.mutatesExternalState());
    }

    @Test
    void changedPayloadCannotReuseIdempotencyMetadata() {
        IntegrationAction original = action(Map.of("issue", "RND-42"));

        assertThrows(
                IllegalArgumentException.class,
                () -> new IntegrationAction(
                        original.actionId(),
                        original.connector(),
                        original.operation(),
                        original.intent(),
                        original.actionKind(),
                        original.classification(),
                        Map.of("issue", "RND-43"),
                        original.origin(),
                        original.createdAt(),
                        original.idempotency()
                )
        );
    }

    @Test
    void secretFieldsAreRejectedEvenWhenNested() {
        Map<String, Object> parameters = Map.of(
                "issue", "RND-42",
                "connection", Map.of("api_token", "must-not-cross-the-bridge")
        );

        assertThrows(IllegalArgumentException.class, () -> action(parameters));
    }

    @Test
    void fingerprintChangesWithTheUserVisibleEffect() {
        IntegrationAction first = action(Map.of("issue", "RND-42"));
        IntegrationAction second = action(Map.of("issue", "RND-43"));

        assertNotEquals(
                first.idempotency().requestFingerprint(),
                second.idempotency().requestFingerprint()
        );
    }

    @Test
    void intentCannotUnderstateTheUserVisibleEffect() {
        IntegrationAction original = action(Map.of("issue", "RND-42"));

        assertThrows(
                IllegalArgumentException.class,
                () -> new IntegrationAction(
                        original.actionId(),
                        original.connector(),
                        original.operation(),
                        IntegrationIntent.READ,
                        ActionKind.DELETE_DATA,
                        original.classification(),
                        original.parameters(),
                        original.origin(),
                        original.createdAt(),
                        original.idempotency()
                )
        );
    }

    private static IntegrationAction action(Map<String, ?> parameters) {
        String requestId = "request-20260831-0001";
        return IntegrationAction.create(
                UUID.fromString("31c8e7e1-14ec-4b50-bfeb-62914c1fe2db"),
                "jira",
                "issue.update",
                IntegrationIntent.WRITE,
                ActionKind.ASSIGN_WORK_ITEM,
                DataClassification.CORPORATE_INTERNAL,
                parameters,
                new ActionOrigin("test-user", OriginType.ASSISTANT, requestId),
                NOW,
                "jira:RND-42:version-7"
        );
    }
}
