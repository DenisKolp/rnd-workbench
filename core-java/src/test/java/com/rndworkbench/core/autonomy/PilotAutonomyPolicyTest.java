package com.rndworkbench.core.autonomy;

import org.junit.jupiter.api.Test;

import java.util.Arrays;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PilotAutonomyPolicyTest {
    private final PilotAutonomyPolicy policy = new PilotAutonomyPolicy();

    @Test
    void everyActionHasAnExplicitPolicy() {
        assertTrue(Arrays.stream(ActionKind.values())
                .map(policy::decide)
                .allMatch(decision -> decision != null));
    }

    @Test
    void analysisAndDraftsRunWithoutConfirmation() {
        assertEquals(
                AutonomyLevel.ALLOW,
                policy.decide(ActionKind.SEARCH_AND_ANALYZE).level()
        );
        assertEquals(
                AutonomyLevel.ALLOW,
                policy.decide(ActionKind.CREATE_DRAFT).level()
        );
    }

    @Test
    void personalTasksRequireNotificationAndUndo() {
        AutonomyDecision decision = policy.decide(ActionKind.UPDATE_PERSONAL_TASK);

        assertEquals(AutonomyLevel.ALLOW_WITH_NOTIFICATION_AND_UNDO, decision.level());
        assertTrue(decision.notificationRequired());
        assertTrue(decision.undoRequired());
        assertFalse(decision.previewRequired());
    }

    @Test
    void messagesAndAssignedTasksRequirePreview() {
        assertEquals(
                AutonomyLevel.REQUIRE_PREVIEW,
                policy.decide(ActionKind.SEND_MESSAGE_OR_EMAIL).level()
        );
        assertTrue(policy.decide(ActionKind.ASSIGN_WORK_ITEM).previewRequired());
    }

    @Test
    void destructiveAndPermissionActionsRequireExplicitConfirmation() {
        AutonomyDecision delete = policy.decide(ActionKind.DELETE_DATA);
        AutonomyDecision permissions = policy.decide(ActionKind.CHANGE_PERMISSIONS);

        assertTrue(delete.explicitConfirmationRequired());
        assertTrue(delete.previewRequired());
        assertTrue(permissions.explicitConfirmationRequired());
    }
}
