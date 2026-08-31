package com.rndworkbench.core.ipc;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CoreIpcApplicationTest {
    @TempDir
    Path temporaryDirectory;

    @Test
    void executableProcessesMultipleJsonlFramesWithoutStderrLogs() {
        String input = """
                {"version":"1.0","type":"health.check","correlationId":"smoke-1","payload":{}}
                {"version":"1.0","type":"route.decide","correlationId":"smoke-2","payload":{"classification":"PUBLIC","preference":"AUTO","availableRoutes":{"local":true,"corporate":false,"external":false},"corporateScopeAuthorized":false,"explicitExternalConsent":false}}
                """;
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        ByteArrayOutputStream error = new ByteArrayOutputStream();

        int exitCode = CoreIpcApplication.run(
                new String[]{
                        "--journal",
                        temporaryDirectory.resolve("app.sqlite").toString()
                },
                new ByteArrayInputStream(input.getBytes(StandardCharsets.UTF_8)),
                output,
                error
        );

        assertEquals(0, exitCode);
        assertEquals(2, output.toString(StandardCharsets.UTF_8).lines().count());
        assertTrue(output.toString(StandardCharsets.UTF_8).contains("\"status\":\"ready\""));
        assertTrue(output.toString(StandardCharsets.UTF_8).contains("LOCAL_SELECTED"));
        assertEquals("", error.toString(StandardCharsets.UTF_8));
    }

    @Test
    void oversizedFrameDoesNotBreakTheNextRequest() {
        String input = "x".repeat(IpcProcessor.MAX_FRAME_CHARACTERS + 10)
                + "\n"
                + "{\"version\":\"1.0\",\"type\":\"health.check\","
                + "\"correlationId\":\"after-large\",\"payload\":{}}\n";
        ByteArrayOutputStream output = new ByteArrayOutputStream();

        int exitCode = CoreIpcApplication.run(
                new String[]{
                        "--journal",
                        temporaryDirectory.resolve("large.sqlite").toString()
                },
                new ByteArrayInputStream(input.getBytes(StandardCharsets.UTF_8)),
                output,
                new ByteArrayOutputStream()
        );

        String text = output.toString(StandardCharsets.UTF_8);
        assertEquals(0, exitCode);
        assertEquals(2, text.lines().count());
        assertTrue(text.contains("MESSAGE_TOO_LARGE"));
        assertTrue(text.contains("after-large"));
    }

    @Test
    void invalidArgumentsReturnOnlyStaticSafeDiagnostic() {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        ByteArrayOutputStream error = new ByteArrayOutputStream();

        int exitCode = CoreIpcApplication.run(
                new String[]{"--unknown", "private-value"},
                new ByteArrayInputStream(new byte[0]),
                output,
                error
        );

        assertEquals(2, exitCode);
        assertEquals("", output.toString(StandardCharsets.UTF_8));
        assertTrue(error.toString(StandardCharsets.UTF_8).startsWith(
                "IPC_ARGUMENTS_INVALID"
        ));
        assertFalse(error.toString(StandardCharsets.UTF_8).contains("private-value"));
    }
}
