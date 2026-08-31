package com.rndworkbench.core.meeting;

import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class SynapseMeetingPackagePolicyTest {
    private static final String TRANSCRIPT_SHA = "a".repeat(64);
    private static final String DESCRIPTION_SHA = "b".repeat(64);
    private static final String ATTACHMENT_SHA = "c".repeat(64);

    private final SynapseMeetingPackagePolicy policy = new SynapseMeetingPackagePolicy();

    @Test
    void localImportPlanIsDeterministicAndNeverClaimsCorporateApi() {
        SynapseMeetingPackagePolicy.Request first = request(
                List.of(transcript(), description(), attachment()),
                new LinkedHashMap<>(Map.of("project", "pilot", "duration_seconds", "1800"))
        );
        SynapseMeetingPackagePolicy.Request reordered = request(
                List.of(attachment(), description(), transcript()),
                new LinkedHashMap<>(Map.of("duration_seconds", "1800", "project", "pilot"))
        );

        SynapseMeetingPackagePolicy.Plan plan = policy.plan(first);

        assertEquals(policy.fingerprint(first), policy.fingerprint(reordered));
        assertEquals(
                "3ce465e6d824b03dc88f192131365e59db5cb3851e0bc4fe3bb57a1648aba550",
                policy.fingerprint(first)
        );
        assertEquals(policy.fingerprint(first), plan.packageFingerprint());
        assertEquals(
                SynapseMeetingPackagePolicy.FINGERPRINT_PROFILE,
                plan.fingerprintProfile()
        );
        assertEquals(64, plan.packageFingerprint().length());
        assertTrue(plan.packageImportAvailable());
        assertFalse(plan.corporateApiConnected());
        assertFalse(plan.realIntegration());
        assertFalse(plan.writeBackAvailable());
        assertFalse(plan.liveConnectorAvailable());
        assertFalse(plan.checkpointAccepted());
        assertEquals(List.of("POLLING", "WEBHOOK"), plan.supportedDeliveryModes());
        assertEquals("CORPORATE_API_NOT_CONNECTED", plan.reasonCode());
        assertEquals(
                List.of(
                        "prepare_next_meeting",
                        "analyze_decisions",
                        "analyze_actions",
                        "analyze_risks",
                        "analyze_questions",
                        "propose_follow_ups"
                ),
                plan.followUpCapabilities().stream()
                        .map(SynapseMeetingPackagePolicy.FollowUpCapability::id)
                        .toList()
        );
        assertEquals(
                "DRAFT_ONLY",
                plan.followUpCapabilities().getLast().availability()
        );
    }

    @Test
    void packageRequiresTranscriptDescriptionAndSafeUniquePaths() {
        assertThrows(
                IllegalArgumentException.class,
                () -> request(List.of(transcript()), Map.of())
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> request(List.of(transcript(), description(), description()), Map.of())
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> new SynapseMeetingPackagePolicy.Part(
                        SynapseMeetingPackagePolicy.PartRole.ATTACHMENT,
                        "../outside.txt",
                        "Outside",
                        "text/plain",
                        ATTACHMENT_SHA,
                        5
                )
        );
    }

    @Test
    void identityFieldsChangeFingerprintWhileParticipantOrderDoesNot() {
        SynapseMeetingPackagePolicy.Request base = request(
                List.of(transcript(), description(), attachment()),
                Map.of("project", "pilot")
        );
        SynapseMeetingPackagePolicy.Request reordered = requestWithIdentity(
                "Анна",
                "confidential",
                List.of("Олег", "Анна", "Иван", "Анна"),
                List.of(transcript(), description(), attachment()),
                Map.of("project", "pilot")
        );
        SynapseMeetingPackagePolicy.Part renamedAttachment =
                new SynapseMeetingPackagePolicy.Part(
                        SynapseMeetingPackagePolicy.PartRole.ATTACHMENT,
                        "attachments/plan.md",
                        "Другой план",
                        "text/markdown",
                        ATTACHMENT_SHA,
                        512
                );

        assertEquals(policy.fingerprint(base), policy.fingerprint(reordered));
        assertFalse(policy.fingerprint(base).equals(policy.fingerprint(
                requestWithIdentity(
                        "Наталья",
                        "confidential",
                        base.participants(),
                        base.parts(),
                        base.metadata()
                )
        )));
        assertFalse(policy.fingerprint(base).equals(policy.fingerprint(
                requestWithIdentity(
                        "Анна",
                        "restricted",
                        base.participants(),
                        base.parts(),
                        base.metadata()
                )
        )));
        assertFalse(policy.fingerprint(base).equals(policy.fingerprint(
                requestWithIdentity(
                        "Анна",
                        "confidential",
                        List.of("Анна", "Иван", "Мария", "Олег"),
                        base.parts(),
                        base.metadata()
                )
        )));
        assertFalse(policy.fingerprint(base).equals(policy.fingerprint(
                requestWithIdentity(
                        "Анна",
                        "confidential",
                        base.participants(),
                        List.of(transcript(), description(), renamedAttachment),
                        base.metadata()
                )
        )));
    }

    @Test
    void secretLikeMetadataAndNonLocalModeAreRejected() {
        assertThrows(
                IllegalArgumentException.class,
                () -> request(
                        List.of(transcript(), description()),
                        Map.of("api_token", "must-not-enter-contract")
                )
        );
        assertThrows(
                IllegalArgumentException.class,
                () -> new SynapseMeetingPackagePolicy.Request(
                        "1.0",
                        "synapse",
                        "LIVE_API",
                        "synapse-demo-42",
                        "Статус пилота",
                        "2026-08-31T10:00:00+03:00",
                        "Анна",
                        "confidential",
                        List.of("Анна", "Иван", "Олег"),
                        null,
                        List.of(transcript(), description()),
                        Map.of()
                )
        );
    }

    @Test
    void pollingOrWebhookCheckpointIsReceiptNotContentIdentity() {
        SynapseMeetingPackagePolicy.Request polling = new SynapseMeetingPackagePolicy.Request(
                "1.0",
                "synapse",
                "LOCAL_PACKAGE_IMPORT",
                "synapse-demo-42",
                "Статус пилота",
                "2026-08-31T10:00:00+03:00",
                "Анна",
                "confidential",
                List.of("Олег", "Анна", "Иван", "Анна"),
                new SynapseMeetingPackagePolicy.ConnectorCheckpoint(
                        SynapseMeetingPackagePolicy.DeliveryMode.POLLING,
                        "cursor-00042",
                        "2026-08-31T07:00:00Z"
                ),
                List.of(transcript(), description()),
                Map.of("project", "pilot")
        );

        SynapseMeetingPackagePolicy.Plan plan = policy.plan(polling);

        assertTrue(plan.checkpointAccepted());
        assertFalse(plan.liveConnectorAvailable());
        assertFalse(plan.corporateApiConnected());
        assertEquals(
                "654c3748e15d8b9017b2aac92d4a5b7105a9c0ec4b81a63bcf7879b006befdcc",
                plan.packageFingerprint()
        );
        assertTrue(policy.fingerprint(polling).equals(policy.fingerprint(
                request(List.of(transcript(), description()), Map.of("project", "pilot"))
        )));
    }

    private static SynapseMeetingPackagePolicy.Request request(
            List<SynapseMeetingPackagePolicy.Part> parts,
            Map<String, String> metadata
    ) {
        return requestWithIdentity(
                "Анна",
                "confidential",
                List.of("Анна", "Иван", "Олег"),
                parts,
                metadata
        );
    }

    private static SynapseMeetingPackagePolicy.Request requestWithIdentity(
            String organizer,
            String classification,
            List<String> participants,
            List<SynapseMeetingPackagePolicy.Part> parts,
            Map<String, String> metadata
    ) {
        return new SynapseMeetingPackagePolicy.Request(
                "1.0",
                "synapse",
                "LOCAL_PACKAGE_IMPORT",
                "synapse-demo-42",
                "Статус пилота",
                "2026-08-31T10:00:00+03:00",
                organizer,
                classification,
                participants,
                null,
                parts,
                metadata
        );
    }

    private static SynapseMeetingPackagePolicy.Part transcript() {
        return new SynapseMeetingPackagePolicy.Part(
                SynapseMeetingPackagePolicy.PartRole.TRANSCRIPT,
                "transcript.txt",
                "Транскрипт",
                "text/plain",
                TRANSCRIPT_SHA,
                1200
        );
    }

    private static SynapseMeetingPackagePolicy.Part description() {
        return new SynapseMeetingPackagePolicy.Part(
                SynapseMeetingPackagePolicy.PartRole.DESCRIPTION,
                "description.md",
                "Описание",
                "text/markdown",
                DESCRIPTION_SHA,
                320
        );
    }

    private static SynapseMeetingPackagePolicy.Part attachment() {
        return new SynapseMeetingPackagePolicy.Part(
                SynapseMeetingPackagePolicy.PartRole.ATTACHMENT,
                "attachments/plan.md",
                "План запуска",
                "text/markdown",
                ATTACHMENT_SHA,
                512
        );
    }
}
