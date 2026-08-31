package com.rndworkbench.core.meeting;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;
import java.util.regex.Pattern;

/**
 * Cross-platform contract for an exported eXpress meeting package.
 *
 * <p>This policy deliberately plans a local package import only. It never claims
 * that a live corporate API is connected and never authorizes a write back to
 * eXpress (legacy source alias {@code synapse}) or another corporate system.</p>
 */
public final class SynapseMeetingPackagePolicy {
    public static final String SCHEMA_VERSION = "1.0";
    public static final String SOURCE_SYSTEM = "synapse";
    public static final String IMPORT_MODE = "LOCAL_PACKAGE_IMPORT";
    public static final String FINGERPRINT_PROFILE =
            "synapse-meeting-package-fingerprint-v2";
    public static final int MAX_ATTACHMENTS = 32;
    public static final long MAX_TOTAL_BYTES = 250L * 1024L * 1024L;

    private static final Pattern PACKAGE_ID =
            Pattern.compile("[A-Za-z0-9][A-Za-z0-9._:-]{2,127}");
    private static final Pattern SHA_256 = Pattern.compile("[a-f0-9]{64}");
    private static final Pattern MEDIA_TYPE =
            Pattern.compile("[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}");
    private static final Pattern METADATA_KEY =
            Pattern.compile("[A-Za-z0-9][A-Za-z0-9._:-]{0,63}");
    private static final Pattern SECRET_KEY = Pattern.compile(
            "(?i)(?:^|[._:-])(secret|token|password|api[_-]?key|authorization|cookie)(?:$|[._:-])"
    );

    /** Roles are fixed so every shell produces the same provenance graph. */
    public enum PartRole {
        TRANSCRIPT,
        DESCRIPTION,
        ATTACHMENT
    }

    /** Future live connectors may deliver the same contract by polling or webhook. */
    public enum DeliveryMode {
        POLLING,
        WEBHOOK
    }

    /**
     * Opaque source checkpoint carried by an export. It is stored only after a
     * successful local import; the current product does not advance it remotely.
     */
    public record ConnectorCheckpoint(
            DeliveryMode deliveryMode,
            String cursor,
            String watermark
    ) {
        public ConnectorCheckpoint {
            Objects.requireNonNull(deliveryMode, "deliveryMode");
            cursor = optionalOpaque(cursor, "cursor");
            watermark = optionalOpaque(watermark, "watermark");
            if (cursor == null && watermark == null) {
                throw new IllegalArgumentException(
                        "Connector checkpoint needs a cursor or watermark"
                );
            }
        }
    }

    /** One immutable content part from the local export package. */
    public record Part(
            PartRole role,
            String relativePath,
            String title,
            String mediaType,
            String sha256,
            long sizeBytes
    ) {
        public Part {
            Objects.requireNonNull(role, "role");
            relativePath = requireText(relativePath, "relativePath", 240);
            title = requireText(title, "title", 240);
            mediaType = requireText(mediaType, "mediaType", 128).toLowerCase(Locale.ROOT);
            sha256 = requireText(sha256, "sha256", 64).toLowerCase(Locale.ROOT);
            if (!safeRelativePath(relativePath)) {
                throw new IllegalArgumentException("relativePath must stay inside the package");
            }
            if (!MEDIA_TYPE.matcher(mediaType).matches()) {
                throw new IllegalArgumentException("mediaType has an invalid format");
            }
            if (!SHA_256.matcher(sha256).matches()) {
                throw new IllegalArgumentException("sha256 has an invalid format");
            }
            if (sizeBytes <= 0 || sizeBytes > MAX_TOTAL_BYTES) {
                throw new IllegalArgumentException("sizeBytes is outside the package limit");
            }
        }
    }

    /** Safe manifest metadata required before Python/native code reads content. */
    public record Request(
            String schemaVersion,
            String sourceSystem,
            String importMode,
            String packageId,
            String title,
            String occurredAt,
            String organizer,
            String classification,
            List<String> participants,
            ConnectorCheckpoint connectorCheckpoint,
            List<Part> parts,
            Map<String, String> metadata
    ) {
        public Request {
            schemaVersion = requireText(schemaVersion, "schemaVersion", 16);
            sourceSystem = requireText(sourceSystem, "sourceSystem", 32)
                    .toLowerCase(Locale.ROOT);
            importMode = requireText(importMode, "importMode", 40)
                    .toUpperCase(Locale.ROOT);
            packageId = requireText(packageId, "packageId", 128);
            title = requireText(title, "title", 240);
            occurredAt = occurredAt == null || occurredAt.isBlank()
                    ? null
                    : validateOccurredAt(occurredAt);
            organizer = organizer == null || organizer.isBlank()
                    ? null
                    : requireText(organizer, "organizer", 160);
            classification = normalizeClassification(classification);
            participants = normalizeParticipants(participants);
            parts = List.copyOf(Objects.requireNonNull(parts, "parts"));
            metadata = normalizeMetadata(metadata);

            if (!SCHEMA_VERSION.equals(schemaVersion)) {
                throw new IllegalArgumentException("Unsupported meeting package schema version");
            }
            if (!SOURCE_SYSTEM.equals(sourceSystem)) {
                throw new IllegalArgumentException(
                        "Only an eXpress package with the legacy synapse alias is supported"
                );
            }
            if (!IMPORT_MODE.equals(importMode)) {
                throw new IllegalArgumentException("Only local package import is supported");
            }
            if (!PACKAGE_ID.matcher(packageId).matches()) {
                throw new IllegalArgumentException("packageId has an invalid format");
            }
            validateParts(parts);
        }
    }

    /** A deterministic capability exposed after a successful local import. */
    public record FollowUpCapability(
            String id,
            String availability,
            String effect
    ) {
        public FollowUpCapability {
            id = requireText(id, "id", 80);
            availability = requireText(availability, "availability", 32);
            effect = requireText(effect, "effect", 32);
        }
    }

    /** Machine-readable plan returned to every desktop shell. */
    public record Plan(
            String schemaVersion,
            String packageId,
            String packageFingerprint,
            String fingerprintProfile,
            String importMode,
            boolean packageImportAvailable,
            boolean corporateApiConnected,
            boolean realIntegration,
            boolean writeBackAvailable,
            boolean liveConnectorAvailable,
            boolean checkpointAccepted,
            List<String> supportedDeliveryModes,
            String reasonCode,
            List<FollowUpCapability> followUpCapabilities
    ) {
        public Plan {
            supportedDeliveryModes = List.copyOf(supportedDeliveryModes);
            followUpCapabilities = List.copyOf(followUpCapabilities);
        }
    }

    /** Validate the manifest and return the same ordered capability set on every OS. */
    public Plan plan(Request request) {
        Objects.requireNonNull(request, "request");
        return new Plan(
                SCHEMA_VERSION,
                request.packageId(),
                fingerprint(request),
                FINGERPRINT_PROFILE,
                IMPORT_MODE,
                true,
                false,
                false,
                false,
                false,
                request.connectorCheckpoint() != null,
                List.of("POLLING", "WEBHOOK"),
                "CORPORATE_API_NOT_CONNECTED",
                List.of(
                        new FollowUpCapability(
                                "prepare_next_meeting",
                                "AVAILABLE_LOCAL",
                                "READ_ONLY"
                        ),
                        new FollowUpCapability(
                                "analyze_decisions",
                                "AVAILABLE_LOCAL",
                                "READ_ONLY"
                        ),
                        new FollowUpCapability(
                                "analyze_actions",
                                "AVAILABLE_LOCAL",
                                "READ_ONLY"
                        ),
                        new FollowUpCapability(
                                "analyze_risks",
                                "AVAILABLE_LOCAL",
                                "READ_ONLY"
                        ),
                        new FollowUpCapability(
                                "analyze_questions",
                                "AVAILABLE_LOCAL",
                                "READ_ONLY"
                        ),
                        new FollowUpCapability(
                                "propose_follow_ups",
                                "DRAFT_ONLY",
                                "LOCAL_DRAFT"
                        )
                )
        );
    }

    /** Stable SHA-256 over normalized fields; part and metadata order do not matter. */
    public String fingerprint(Request request) {
        Objects.requireNonNull(request, "request");
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            update(digest, FINGERPRINT_PROFILE);
            update(digest, request.schemaVersion());
            update(digest, request.sourceSystem());
            update(digest, request.importMode());
            update(digest, request.packageId());
            update(digest, request.title());
            update(digest, request.occurredAt() == null ? "" : request.occurredAt());
            update(digest, request.organizer() == null ? "" : request.organizer());
            update(digest, request.classification());
            update(digest, Integer.toString(request.participants().size()));
            request.participants().forEach(participant -> update(digest, participant));
            update(digest, Integer.toString(request.metadata().size()));
            request.metadata().forEach((key, value) -> {
                update(digest, key);
                update(digest, value);
            });
            List<Part> orderedParts = request.parts().stream()
                    .sorted(Comparator.comparing((Part part) -> part.role().name())
                            .thenComparing(Part::relativePath))
                    .toList();
            update(digest, Integer.toString(orderedParts.size()));
            orderedParts.forEach(part -> {
                        update(digest, part.role().name());
                        update(digest, part.relativePath());
                        update(digest, part.title());
                        update(digest, part.mediaType());
                        update(digest, part.sha256());
                        update(digest, Long.toString(part.sizeBytes()));
                    });
            return HexFormat.of().formatHex(digest.digest());
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }

    private static void validateParts(List<Part> parts) {
        if (parts.isEmpty()) {
            throw new IllegalArgumentException("The meeting package has no parts");
        }
        long transcriptCount = parts.stream()
                .filter(part -> part.role() == PartRole.TRANSCRIPT)
                .count();
        long descriptionCount = parts.stream()
                .filter(part -> part.role() == PartRole.DESCRIPTION)
                .count();
        long attachmentCount = parts.stream()
                .filter(part -> part.role() == PartRole.ATTACHMENT)
                .count();
        if (transcriptCount != 1 || descriptionCount != 1) {
            throw new IllegalArgumentException(
                    "The package needs exactly one transcript and one description"
            );
        }
        if (attachmentCount > MAX_ATTACHMENTS) {
            throw new IllegalArgumentException("The package has too many attachments");
        }
        long total = 0;
        List<String> paths = new ArrayList<>();
        for (Part part : parts) {
            total = Math.addExact(total, part.sizeBytes());
            if (total > MAX_TOTAL_BYTES) {
                throw new IllegalArgumentException("The package exceeds the total size limit");
            }
            if (paths.contains(part.relativePath())) {
                throw new IllegalArgumentException("Part paths must be unique");
            }
            paths.add(part.relativePath());
        }
    }

    private static Map<String, String> normalizeMetadata(Map<String, String> metadata) {
        Objects.requireNonNull(metadata, "metadata");
        if (metadata.size() > 64) {
            throw new IllegalArgumentException("metadata has too many entries");
        }
        TreeMap<String, String> normalized = new TreeMap<>();
        metadata.forEach((rawKey, rawValue) -> {
            String key = requireText(rawKey, "metadata key", 64);
            String value = requireText(rawValue, "metadata value", 512);
            if (!METADATA_KEY.matcher(key).matches() || SECRET_KEY.matcher(key).find()) {
                throw new IllegalArgumentException("metadata key is not allowed");
            }
            normalized.put(key, value);
        });
        return Collections.unmodifiableMap(normalized);
    }

    private static List<String> normalizeParticipants(List<String> participants) {
        Objects.requireNonNull(participants, "participants");
        if (participants.size() > 200) {
            throw new IllegalArgumentException("participants has too many entries");
        }
        TreeMap<String, String> normalized = new TreeMap<>();
        participants.forEach(rawValue -> {
            String value = requireText(rawValue, "participant", 160);
            String key = value.toLowerCase(Locale.ROOT);
            normalized.merge(
                    key,
                    value,
                    (first, second) -> first.compareTo(second) <= 0 ? first : second
            );
        });
        return List.copyOf(normalized.values());
    }

    private static String normalizeClassification(String classification) {
        String normalized = requireText(classification, "classification", 32)
                .toLowerCase(Locale.ROOT);
        if (!List.of("public", "internal", "confidential", "restricted")
                .contains(normalized)) {
            throw new IllegalArgumentException("classification is not supported");
        }
        return normalized;
    }

    private static String validateOccurredAt(String value) {
        String normalized = requireText(value, "occurredAt", 64);
        try {
            if (normalized.length() == 10) {
                LocalDate.parse(normalized);
            } else {
                OffsetDateTime.parse(normalized);
            }
            return normalized;
        } catch (DateTimeParseException exception) {
            throw new IllegalArgumentException("occurredAt must be ISO-8601", exception);
        }
    }

    private static boolean safeRelativePath(String value) {
        if (value.startsWith("/") || value.startsWith("\\") || value.contains("\\")) {
            return false;
        }
        String[] parts = value.split("/", -1);
        if (parts.length == 0) {
            return false;
        }
        for (String part : parts) {
            if (part.isBlank() || ".".equals(part) || "..".equals(part)) {
                return false;
            }
        }
        return true;
    }

    private static String requireText(String value, String label, int maxLength) {
        if (value == null || value.isBlank() || value.length() > maxLength) {
            throw new IllegalArgumentException(label + " has an invalid format");
        }
        return value.strip();
    }

    private static String optionalOpaque(String value, String label) {
        if (value == null || value.isBlank()) {
            return null;
        }
        String normalized = value.strip();
        if (normalized.length() > 512 || normalized.chars().anyMatch(Character::isISOControl)) {
            throw new IllegalArgumentException(label + " has an invalid format");
        }
        return normalized;
    }

    private static void update(MessageDigest digest, String value) {
        byte[] bytes = value.getBytes(StandardCharsets.UTF_8);
        digest.update(ByteBuffer.allocate(Integer.BYTES).putInt(bytes.length).array());
        digest.update(bytes);
    }
}
