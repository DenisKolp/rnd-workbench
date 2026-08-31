package com.rndworkbench.core.autonomy;

import java.util.EnumMap;
import java.util.Map;
import java.util.Objects;

/** Deterministic confirmation policy agreed for the pilot. */
public final class PilotAutonomyPolicy {
    private static final Map<ActionKind, AutonomyDecision> DECISIONS = decisions();

    public AutonomyDecision decide(ActionKind actionKind) {
        Objects.requireNonNull(actionKind, "actionKind");
        return DECISIONS.get(actionKind);
    }

    private static Map<ActionKind, AutonomyDecision> decisions() {
        EnumMap<ActionKind, AutonomyDecision> result =
                new EnumMap<>(ActionKind.class);

        allow(result, ActionKind.READ_CONTEXT);
        allow(result, ActionKind.SEARCH_AND_ANALYZE);
        allow(result, ActionKind.TRANSCRIBE_AUDIO);
        allow(result, ActionKind.UPDATE_WORKING_MEMORY);
        allow(result, ActionKind.CREATE_DRAFT);
        allow(result, ActionKind.SELECT_MODEL);

        notifyAndUndo(result, ActionKind.UPDATE_PERSONAL_TASK);
        notifyAndUndo(result, ActionKind.UPDATE_INTERNAL_MATERIAL);
        notifyAndUndo(result, ActionKind.CLASSIFY_WORKSPACE_CONTENT);
        notifyAndUndo(result, ActionKind.RUN_PREAUTHORIZED_AUTOMATION);

        preview(result, ActionKind.SEND_MESSAGE_OR_EMAIL);
        preview(result, ActionKind.UPSERT_CALENDAR_EVENT);
        preview(result, ActionKind.ASSIGN_WORK_ITEM);
        preview(result, ActionKind.UPSERT_KNOWLEDGE_PAGE);
        preview(result, ActionKind.EXTERNAL_WRITE);

        explicit(result, ActionKind.DELETE_DATA);
        explicit(result, ActionKind.MASS_OPERATION);
        explicit(result, ActionKind.CHANGE_PERMISSIONS);
        explicit(result, ActionKind.PUBLISH_EXTERNAL);
        explicit(result, ActionKind.IRREVERSIBLE_HIGH_RISK);

        if (result.size() != ActionKind.values().length) {
            throw new IllegalStateException("Every action kind must have a policy");
        }
        return Map.copyOf(result);
    }

    private static void allow(
            EnumMap<ActionKind, AutonomyDecision> target,
            ActionKind actionKind
    ) {
        target.put(actionKind, new AutonomyDecision(
                AutonomyLevel.ALLOW,
                false,
                false,
                false,
                false,
                "pilot.allow"
        ));
    }

    private static void notifyAndUndo(
            EnumMap<ActionKind, AutonomyDecision> target,
            ActionKind actionKind
    ) {
        target.put(actionKind, new AutonomyDecision(
                AutonomyLevel.ALLOW_WITH_NOTIFICATION_AND_UNDO,
                true,
                true,
                false,
                false,
                "pilot.notify-and-undo"
        ));
    }

    private static void preview(
            EnumMap<ActionKind, AutonomyDecision> target,
            ActionKind actionKind
    ) {
        target.put(actionKind, new AutonomyDecision(
                AutonomyLevel.REQUIRE_PREVIEW,
                true,
                false,
                true,
                false,
                "pilot.preview"
        ));
    }

    private static void explicit(
            EnumMap<ActionKind, AutonomyDecision> target,
            ActionKind actionKind
    ) {
        target.put(actionKind, new AutonomyDecision(
                AutonomyLevel.REQUIRE_EXPLICIT_CONFIRMATION,
                true,
                false,
                true,
                true,
                "pilot.explicit-confirmation"
        ));
    }
}
