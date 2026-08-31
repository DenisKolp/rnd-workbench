package com.rndworkbench.core.integration;

import com.rndworkbench.core.autonomy.ActionKind;
import com.rndworkbench.core.data.DataClassification;

import java.time.Instant;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * Vendor-neutral action envelope crossing from Java policy code to a connector.
 * Credentials are forbidden in {@code parameters} and belong in a platform
 * secret store owned by the connector process.
 */
public record IntegrationAction(
        UUID actionId,
        String connector,
        String operation,
        IntegrationIntent intent,
        ActionKind actionKind,
        DataClassification classification,
        Map<String, Object> parameters,
        ActionOrigin origin,
        Instant createdAt,
        IdempotencyMetadata idempotency
) {
    private static final Pattern CONNECTOR = Pattern.compile("[a-z0-9][a-z0-9_-]{0,63}");
    private static final Pattern OPERATION = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]{0,127}");

    public IntegrationAction {
        Objects.requireNonNull(actionId, "actionId");
        connector = requireMatch(connector, "connector", CONNECTOR);
        operation = requireMatch(operation, "operation", OPERATION);
        Objects.requireNonNull(intent, "intent");
        Objects.requireNonNull(actionKind, "actionKind");
        validateIntentActionKind(intent, actionKind);
        Objects.requireNonNull(classification, "classification");
        parameters = IntegrationActionContract.freezeParameters(parameters);
        Objects.requireNonNull(origin, "origin");
        Objects.requireNonNull(createdAt, "createdAt");
        Objects.requireNonNull(idempotency, "idempotency");

        String expectedFingerprint = IntegrationActionContract.fingerprint(
                connector,
                operation,
                intent,
                actionKind,
                classification,
                parameters
        );
        if (!expectedFingerprint.equals(idempotency.requestFingerprint())) {
            throw new IllegalArgumentException(
                    "Idempotency fingerprint does not match the action payload"
            );
        }
        if (!origin.requestId().equals(idempotency.correlationId())) {
            throw new IllegalArgumentException(
                    "Origin requestId must match the idempotency correlationId"
            );
        }
        if (!createdAt.equals(idempotency.issuedAt())) {
            throw new IllegalArgumentException(
                    "Action creation time must match idempotency issue time"
            );
        }
    }

    public static IntegrationAction create(
            UUID actionId,
            String connector,
            String operation,
            IntegrationIntent intent,
            ActionKind actionKind,
            DataClassification classification,
            Map<String, ?> parameters,
            ActionOrigin origin,
            Instant createdAt,
            String idempotencyKey
    ) {
        Map<String, Object> frozen = IntegrationActionContract.freezeParameters(parameters);
        String fingerprint = IntegrationActionContract.fingerprint(
                connector,
                operation,
                intent,
                actionKind,
                classification,
                frozen
        );
        IdempotencyMetadata metadata = new IdempotencyMetadata(
                idempotencyKey,
                fingerprint,
                origin.requestId(),
                createdAt,
                1
        );
        return new IntegrationAction(
                actionId,
                connector,
                operation,
                intent,
                actionKind,
                classification,
                frozen,
                origin,
                createdAt,
                metadata
        );
    }

    public IntegrationAction nextAttempt() {
        return new IntegrationAction(
                actionId,
                connector,
                operation,
                intent,
                actionKind,
                classification,
                parameters,
                origin,
                createdAt,
                idempotency.nextAttempt()
        );
    }

    public boolean mutatesExternalState() {
        return intent.isMutation();
    }

    private static void validateIntentActionKind(
            IntegrationIntent intent,
            ActionKind actionKind
    ) {
        if (actionKind == ActionKind.IRREVERSIBLE_HIGH_RISK && intent.isMutation()) {
            return;
        }
        boolean compatible = switch (intent) {
            case READ -> actionKind == ActionKind.READ_CONTEXT
                    || actionKind == ActionKind.SEARCH_AND_ANALYZE;
            case DRAFT -> actionKind == ActionKind.CREATE_DRAFT;
            case WRITE -> switch (actionKind) {
                case UPDATE_PERSONAL_TASK,
                        UPDATE_INTERNAL_MATERIAL,
                        CLASSIFY_WORKSPACE_CONTENT,
                        RUN_PREAUTHORIZED_AUTOMATION,
                        SEND_MESSAGE_OR_EMAIL,
                        UPSERT_CALENDAR_EVENT,
                        ASSIGN_WORK_ITEM,
                        UPSERT_KNOWLEDGE_PAGE -> true;
                default -> false;
            };
            case PUBLISH -> actionKind == ActionKind.UPSERT_KNOWLEDGE_PAGE
                    || actionKind == ActionKind.PUBLISH_EXTERNAL;
            case DELETE -> actionKind == ActionKind.DELETE_DATA;
            case PERMISSIONS -> actionKind == ActionKind.CHANGE_PERMISSIONS;
            case MASS -> actionKind == ActionKind.MASS_OPERATION;
        };
        if (!compatible) {
            throw new IllegalArgumentException(
                    "Integration intent and action kind describe different effects"
            );
        }
    }

    private static String requireMatch(
            String value,
            String field,
            Pattern pattern
    ) {
        if (value == null || !pattern.matcher(value).matches()) {
            throw new IllegalArgumentException(field + " has an invalid format");
        }
        return value;
    }
}
