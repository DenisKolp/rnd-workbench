package com.rndworkbench.core.ipc;

import com.rndworkbench.core.journal.ActionClaimResult;
import com.rndworkbench.core.journal.ActionCompletionRequest;
import com.rndworkbench.core.journal.ActionCompletionResult;
import com.rndworkbench.core.journal.ActionExecutionResult;
import com.rndworkbench.core.journal.ActionInspectionRequest;
import com.rndworkbench.core.journal.ActionInspectionResult;
import com.rndworkbench.core.journal.ActionJournal;
import com.rndworkbench.core.routing.PilotModelRoutingPolicy;
import org.junit.jupiter.api.Test;

import java.sql.SQLException;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class IpcProcessorTest {
    private static final String FINGERPRINT = "a".repeat(64);
    private static final String TOKEN = "00000000-0000-4000-8000-000000000001";

    private final IpcProcessor processor = new IpcProcessor(
            PilotModelRoutingPolicy.pilotDefaults(),
            new FixedJournal()
    );

    @Test
    void routeDecisionHasDeterministicJsonAndCorrelation() {
        String response = processor.process("""
                {"version":"1.0","type":"route.decide","correlationId":"req-1","payload":{"classification":"CORPORATE_INTERNAL","preference":"AUTO","availableRoutes":{"local":true,"corporate":true,"external":false},"corporateScopeAuthorized":true,"explicitExternalConsent":false}}
                """.strip());

        assertEquals(
                "{\"correlationId\":\"req-1\",\"ok\":true,\"payload\":{"
                        + "\"localFallbackBeforeFirstOutput\":false,"
                        + "\"reason\":\"LOCAL_SELECTED\","
                        + "\"route\":\"LOCAL\","
                        + "\"status\":\"SELECTED\"},"
                        + "\"type\":\"route.decision\",\"version\":\"1.0\"}",
                response
        );
    }

    @Test
    void autonomyDecisionContainsOnlyTheActionPolicy() {
        String response = processor.process("""
                {"version":"1.0","type":"autonomy.decide","correlationId":"policy-1","payload":{"actionKind":"ASSIGN_WORK_ITEM"}}
                """.strip());

        assertEquals(
                "{\"correlationId\":\"policy-1\",\"ok\":true,\"payload\":{"
                        + "\"explicitConfirmationRequired\":false,"
                        + "\"level\":\"REQUIRE_PREVIEW\","
                        + "\"notificationRequired\":true,"
                        + "\"previewRequired\":true,"
                        + "\"reasonCode\":\"pilot.preview\","
                        + "\"undoRequired\":false},"
                        + "\"type\":\"autonomy.decision\",\"version\":\"1.0\"}",
                response
        );
        assertFalse(response.contains("payloadText"));
        assertFalse(response.contains("assignee"));
    }

    @Test
    void duplicateAndUnknownEnvelopeFieldsAreRejected() {
        String duplicate = processor.process("""
                {"version":"1.0","version":"1.0","type":"health.check","correlationId":"req-2","payload":{}}
                """.strip());
        String unknown = processor.process("""
                {"version":"1.0","type":"health.check","correlationId":"req-2","payload":{},"extra":true}
                """.strip());

        assertTrue(duplicate.contains("\"code\":\"INVALID_JSON\""));
        assertTrue(unknown.contains("\"code\":\"INVALID_ENVELOPE\""));
    }

    @Test
    void versionAndTypeAreValidatedSeparately() {
        String version = processor.process("""
                {"version":"2.0","type":"health.check","correlationId":"req-3","payload":{}}
                """.strip());
        String type = processor.process("""
                {"version":"1.0","type":"unknown.command","correlationId":"req-4","payload":{}}
                """.strip());

        assertTrue(version.contains("\"code\":\"UNSUPPORTED_VERSION\""));
        assertTrue(version.contains("\"correlationId\":\"req-3\""));
        assertTrue(type.contains("\"code\":\"UNSUPPORTED_TYPE\""));
        assertTrue(type.contains("\"correlationId\":\"req-4\""));
    }

    @Test
    void missingOrUnknownPayloadFieldsAreRejected() {
        String missing = processor.process("""
                {"version":"1.0","type":"route.decide","correlationId":"req-5","payload":{"classification":"PUBLIC","preference":"AUTO","availableRoutes":{"local":true,"corporate":false,"external":false},"corporateScopeAuthorized":true}}
                """.strip());
        String unknown = processor.process("""
                {"version":"1.0","type":"health.check","correlationId":"req-6","payload":{"secret":"must-not-echo"}}
                """.strip());
        String unknownAction = processor.process("""
                {"version":"1.0","type":"autonomy.decide","correlationId":"req-6a","payload":{"actionKind":"SEND_WITHOUT_CONFIRMATION"}}
                """.strip());

        assertTrue(missing.contains("\"code\":\"INVALID_PAYLOAD\""));
        assertTrue(unknown.contains("\"code\":\"INVALID_PAYLOAD\""));
        assertFalse(unknown.contains("must-not-echo"));
        assertTrue(unknownAction.contains("\"code\":\"INVALID_PAYLOAD\""));
        assertFalse(unknownAction.contains("SEND_WITHOUT_CONFIRMATION"));
    }

    @Test
    void claimAndCompletionExposeOnlySafeJournalContracts() {
        String claim = processor.process("""
                {"version":"1.0","type":"action.claim","correlationId":"req-7","payload":{"idempotencyKey":"jira:RND-42:version-7","requestFingerprint":"%s"}}
                """.formatted(FINGERPRINT).strip());
        String complete = processor.process("""
                {"version":"1.0","type":"action.complete","correlationId":"req-8","payload":{"idempotencyKey":"jira:RND-42:version-7","requestFingerprint":"%s","claimToken":"%s","outcome":"SUCCESS","resultCode":"ISSUE.UPDATED","externalReference":"RND-42","completedAt":"2026-08-31T13:00:00Z"}}
                """.formatted(FINGERPRINT, TOKEN).strip());

        assertEquals(
                "{\"correlationId\":\"req-7\",\"ok\":true,\"payload\":{"
                        + "\"claimToken\":\"" + TOKEN + "\","
                        + "\"disposition\":\"CLAIMED\"},"
                        + "\"type\":\"action.claim.result\",\"version\":\"1.0\"}",
                claim
        );
        assertTrue(complete.contains("\"disposition\":\"RECORDED\""));
        assertTrue(complete.contains("\"resultCode\":\"ISSUE.UPDATED\""));
    }

    @Test
    void inspectionReturnsOnlyTheOwningTokenForAnInProgressFingerprint() {
        String response = processor.process("""
                {"version":"1.0","type":"action.inspect","correlationId":"req-9","payload":{"idempotencyKey":"jira:RND-42:version-7","requestFingerprint":"%s"}}
                """.formatted(FINGERPRINT).strip());

        assertEquals(
                "{\"correlationId\":\"req-9\",\"ok\":true,\"payload\":{"
                        + "\"claimToken\":\"" + TOKEN + "\","
                        + "\"disposition\":\"IN_PROGRESS\"},"
                        + "\"type\":\"action.inspect.result\",\"version\":\"1.0\"}",
                response
        );
    }

    @Test
    void meetingPackagePlanExposesLocalImportWithoutClaimingLiveSynapse() {
        String response = processor.process("""
                {"version":"1.0","type":"meeting.package.plan","correlationId":"meeting-1","payload":{"schemaVersion":"1.0","sourceSystem":"synapse","importMode":"LOCAL_PACKAGE_IMPORT","packageId":"synapse-demo-42","title":"Статус пилота","occurredAt":"2026-08-31T10:00:00+03:00","organizer":"Анна","classification":"confidential","participants":["Анна","Иван","Олег"],"parts":[{"role":"TRANSCRIPT","relativePath":"transcript.txt","title":"Транскрипт","mediaType":"text/plain","sha256":"%s","sizeBytes":1200},{"role":"DESCRIPTION","relativePath":"description.md","title":"Описание","mediaType":"text/markdown","sha256":"%s","sizeBytes":320}],"metadata":{"project":"pilot"}}}
                """.formatted("a".repeat(64), "b".repeat(64)).strip());

        assertTrue(response.contains("\"type\":\"meeting.package.plan.result\""));
        assertTrue(response.contains("\"packageImportAvailable\":true"));
        assertTrue(response.contains(
                "\"fingerprintProfile\":\"synapse-meeting-package-fingerprint-v2\""
        ));
        assertTrue(response.contains("\"corporateApiConnected\":false"));
        assertTrue(response.contains("\"realIntegration\":false"));
        assertTrue(response.contains("\"writeBackAvailable\":false"));
        assertTrue(response.contains("\"id\":\"prepare_next_meeting\""));
        assertTrue(response.contains("\"availability\":\"DRAFT_ONLY\""));
    }

    @Test
    void meetingPackagePlanRejectsTraversalWithoutEchoingPath() {
        String marker = "private-path-marker";
        String response = processor.process("""
                {"version":"1.0","type":"meeting.package.plan","correlationId":"meeting-2","payload":{"schemaVersion":"1.0","sourceSystem":"synapse","importMode":"LOCAL_PACKAGE_IMPORT","packageId":"synapse-demo-42","title":"Статус пилота","occurredAt":null,"organizer":null,"classification":"internal","participants":[],"parts":[{"role":"TRANSCRIPT","relativePath":"../%s.txt","title":"Транскрипт","mediaType":"text/plain","sha256":"%s","sizeBytes":1200},{"role":"DESCRIPTION","relativePath":"description.md","title":"Описание","mediaType":"text/markdown","sha256":"%s","sizeBytes":320}],"metadata":{}}}
                """.formatted(marker, "a".repeat(64), "b".repeat(64)).strip());

        assertTrue(response.contains("\"code\":\"INVALID_PAYLOAD\""));
        assertFalse(response.contains(marker));
    }

    @Test
    void invalidJsonNeverEchoesInputOrExceptionDetails() {
        String marker = "private-secret-marker";
        String response = processor.process("{\"" + marker + "\":");

        assertTrue(response.contains("\"code\":\"INVALID_JSON\""));
        assertFalse(response.contains(marker));
        assertFalse(response.contains("Exception"));
    }

    @Test
    void oversizedFrameGetsBoundedProtocolError() {
        String response = processor.process("x".repeat(
                IpcProcessor.MAX_FRAME_CHARACTERS + 1
        ));

        assertTrue(response.contains("\"code\":\"MESSAGE_TOO_LARGE\""));
        assertTrue(response.length() < 512);
    }

    private static final class FixedJournal implements ActionJournal {
        @Override
        public ActionClaimResult claim(
                com.rndworkbench.core.journal.ActionClaimRequest request
        ) throws SQLException {
            return ActionClaimResult.claimed(TOKEN);
        }

        @Override
        public ActionCompletionResult complete(ActionCompletionRequest request)
                throws SQLException {
            ActionExecutionResult result = request.result();
            return ActionCompletionResult.recorded(result);
        }

        @Override
        public ActionInspectionResult inspect(ActionInspectionRequest request)
                throws SQLException {
            return ActionInspectionResult.inProgress(TOKEN);
        }
    }
}
