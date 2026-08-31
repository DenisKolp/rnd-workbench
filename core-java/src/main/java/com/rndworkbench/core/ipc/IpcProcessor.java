package com.rndworkbench.core.ipc;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.rndworkbench.core.autonomy.PilotAutonomyPolicy;
import com.rndworkbench.core.ipc.contract.ActionClaimPayload;
import com.rndworkbench.core.ipc.contract.ActionCompletionPayload;
import com.rndworkbench.core.ipc.contract.ActionInspectionPayload;
import com.rndworkbench.core.ipc.contract.AutonomyDecisionRequestPayload;
import com.rndworkbench.core.ipc.contract.EmptyPayload;
import com.rndworkbench.core.ipc.contract.HealthStatusPayload;
import com.rndworkbench.core.ipc.contract.IpcRequestEnvelope;
import com.rndworkbench.core.ipc.contract.IpcResponseEnvelope;
import com.rndworkbench.core.ipc.contract.RouteDecisionRequestPayload;
import com.rndworkbench.core.ipc.contract.RouteDecisionResultPayload;
import com.rndworkbench.core.journal.ActionClaimRequest;
import com.rndworkbench.core.journal.ActionCompletionRequest;
import com.rndworkbench.core.journal.ActionExecutionResult;
import com.rndworkbench.core.journal.ActionInspectionRequest;
import com.rndworkbench.core.journal.ActionJournal;
import com.rndworkbench.core.meeting.SynapseMeetingPackagePolicy;
import com.rndworkbench.core.routing.AvailableRoutes;
import com.rndworkbench.core.routing.PilotModelRoutingPolicy;
import com.rndworkbench.core.routing.RoutingDecision;
import com.rndworkbench.core.routing.RoutingRequest;

import java.sql.SQLException;
import java.util.Objects;

/** Processes one JSONL request without logging its content or exception text. */
public final class IpcProcessor {
    public static final String PROTOCOL_VERSION = "1.0";
    public static final int MAX_FRAME_CHARACTERS = 65_536;
    private static final String UNKNOWN_CORRELATION = "unavailable";

    private final ObjectMapper mapper;
    private final PilotModelRoutingPolicy routingPolicy;
    private final PilotAutonomyPolicy autonomyPolicy;
    private final ActionJournal journal;
    private final SynapseMeetingPackagePolicy meetingPackagePolicy;

    public IpcProcessor(
            PilotModelRoutingPolicy routingPolicy,
            ActionJournal journal
    ) {
        this.routingPolicy = Objects.requireNonNull(routingPolicy, "routingPolicy");
        this.journal = Objects.requireNonNull(journal, "journal");
        autonomyPolicy = new PilotAutonomyPolicy();
        meetingPackagePolicy = new SynapseMeetingPackagePolicy();
        mapper = IpcJson.createMapper();
    }

    public String process(String frame) {
        if (frame == null || frame.isBlank()) {
            return encode(error(
                    UNKNOWN_CORRELATION,
                    "INVALID_JSON",
                    "The frame must contain one JSON object."
            ));
        }
        if (frame.length() > MAX_FRAME_CHARACTERS) {
            return messageTooLargeResponse();
        }

        JsonNode root;
        try {
            root = mapper.readTree(frame);
        } catch (JsonProcessingException exception) {
            return encode(error(
                    UNKNOWN_CORRELATION,
                    "INVALID_JSON",
                    "The frame is not valid strict JSON."
            ));
        }
        if (root == null || !root.isObject()) {
            return encode(error(
                    UNKNOWN_CORRELATION,
                    "INVALID_ENVELOPE",
                    "The request envelope is invalid."
            ));
        }

        IpcRequestEnvelope request;
        try {
            request = mapper.treeToValue(root, IpcRequestEnvelope.class);
        } catch (JsonProcessingException | IllegalArgumentException exception) {
            return encode(error(
                    UNKNOWN_CORRELATION,
                    "INVALID_ENVELOPE",
                    "The request envelope is invalid."
            ));
        }

        if (!PROTOCOL_VERSION.equals(request.version())) {
            return encode(error(
                    request.correlationId(),
                    "UNSUPPORTED_VERSION",
                    "Only protocol version 1.0 is supported."
            ));
        }

        try {
            return encode(dispatch(request));
        } catch (JsonProcessingException | IllegalArgumentException exception) {
            return encode(error(
                    request.correlationId(),
                    "INVALID_PAYLOAD",
                    "The payload is invalid for the requested type."
            ));
        } catch (SQLException exception) {
            return encode(error(
                    request.correlationId(),
                    "JOURNAL_UNAVAILABLE",
                    "The persistent action journal is unavailable."
            ));
        } catch (RuntimeException exception) {
            return encode(error(
                    request.correlationId(),
                    "INTERNAL_ERROR",
                    "The core could not process the request."
            ));
        }
    }

    public String messageTooLargeResponse() {
        return encode(error(
                UNKNOWN_CORRELATION,
                "MESSAGE_TOO_LARGE",
                "The JSONL frame exceeds 65536 characters."
        ));
    }

    private IpcResponseEnvelope dispatch(IpcRequestEnvelope request)
            throws JsonProcessingException, SQLException {
        return switch (request.type()) {
            case "health.check" -> health(request);
            case "route.decide" -> route(request);
            case "autonomy.decide" -> autonomy(request);
            case "meeting.package.plan" -> meetingPackagePlan(request);
            case "action.claim" -> claim(request);
            case "action.inspect" -> inspect(request);
            case "action.complete" -> complete(request);
            default -> error(
                    request.correlationId(),
                    "UNSUPPORTED_TYPE",
                    "The request type is not supported."
            );
        };
    }

    private IpcResponseEnvelope health(IpcRequestEnvelope request)
            throws JsonProcessingException {
        mapper.treeToValue(request.payload(), EmptyPayload.class);
        return success(
                "health.status",
                request.correlationId(),
                new HealthStatusPayload("ready", PROTOCOL_VERSION)
        );
    }

    private IpcResponseEnvelope route(IpcRequestEnvelope request)
            throws JsonProcessingException {
        RouteDecisionRequestPayload payload = mapper.treeToValue(
                request.payload(),
                RouteDecisionRequestPayload.class
        );
        AvailableRoutes available = new AvailableRoutes(
                payload.availableRoutes().local(),
                payload.availableRoutes().corporate(),
                payload.availableRoutes().external()
        );
        RoutingDecision decision = routingPolicy.route(new RoutingRequest(
                payload.classification(),
                payload.preference(),
                available,
                payload.corporateScopeAuthorized(),
                payload.explicitExternalConsent()
        ));
        return success(
                "route.decision",
                request.correlationId(),
                RouteDecisionResultPayload.from(decision)
        );
    }

    private IpcResponseEnvelope claim(IpcRequestEnvelope request)
            throws JsonProcessingException, SQLException {
        ActionClaimPayload payload = mapper.treeToValue(
                request.payload(),
                ActionClaimPayload.class
        );
        return success(
                "action.claim.result",
                request.correlationId(),
                journal.claim(new ActionClaimRequest(
                        payload.idempotencyKey(),
                        payload.requestFingerprint(),
                        request.correlationId()
                ))
        );
    }

    private IpcResponseEnvelope autonomy(IpcRequestEnvelope request)
            throws JsonProcessingException {
        AutonomyDecisionRequestPayload payload = mapper.treeToValue(
                request.payload(),
                AutonomyDecisionRequestPayload.class
        );
        return success(
                "autonomy.decision",
                request.correlationId(),
                autonomyPolicy.decide(payload.actionKind())
        );
    }

    private IpcResponseEnvelope meetingPackagePlan(IpcRequestEnvelope request)
            throws JsonProcessingException {
        SynapseMeetingPackagePolicy.Request payload = mapper.treeToValue(
                request.payload(),
                SynapseMeetingPackagePolicy.Request.class
        );
        return success(
                "meeting.package.plan.result",
                request.correlationId(),
                meetingPackagePolicy.plan(payload)
        );
    }

    private IpcResponseEnvelope inspect(IpcRequestEnvelope request)
            throws JsonProcessingException, SQLException {
        ActionInspectionPayload payload = mapper.treeToValue(
                request.payload(),
                ActionInspectionPayload.class
        );
        return success(
                "action.inspect.result",
                request.correlationId(),
                journal.inspect(new ActionInspectionRequest(
                        payload.idempotencyKey(),
                        payload.requestFingerprint()
                ))
        );
    }

    private IpcResponseEnvelope complete(IpcRequestEnvelope request)
            throws JsonProcessingException, SQLException {
        ActionCompletionPayload payload = mapper.treeToValue(
                request.payload(),
                ActionCompletionPayload.class
        );
        ActionExecutionResult result = new ActionExecutionResult(
                payload.outcome(),
                payload.resultCode(),
                payload.externalReference(),
                payload.completedAt()
        );
        return success(
                "action.complete.result",
                request.correlationId(),
                journal.complete(new ActionCompletionRequest(
                        payload.idempotencyKey(),
                        payload.requestFingerprint(),
                        payload.claimToken(),
                        result
                ))
        );
    }

    private static IpcResponseEnvelope success(
            String type,
            String correlationId,
            Object payload
    ) {
        return IpcResponseEnvelope.success(
                PROTOCOL_VERSION,
                type,
                correlationId,
                payload
        );
    }

    private static IpcResponseEnvelope error(
            String correlationId,
            String code,
            String message
    ) {
        return IpcResponseEnvelope.failure(
                PROTOCOL_VERSION,
                correlationId,
                code,
                message
        );
    }

    private String encode(IpcResponseEnvelope response) {
        try {
            return mapper.writeValueAsString(response);
        } catch (JsonProcessingException exception) {
            throw new IllegalStateException("Failed to encode a protocol response");
        }
    }
}
