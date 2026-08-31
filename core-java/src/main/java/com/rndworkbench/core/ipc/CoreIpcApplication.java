package com.rndworkbench.core.ipc;

import com.rndworkbench.core.journal.SqliteActionJournal;
import com.rndworkbench.core.routing.PilotModelRoutingPolicy;

import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.sql.SQLException;

/** Executable stdio JSONL boundary for desktop clients. */
public final class CoreIpcApplication {
    private static final String USAGE = """
            Usage: rnd-workbench-core --journal <sqlite-file> [--external-models-enabled]
            Reads one JSON request per stdin line and writes one JSON response per stdout line.
            """;

    private CoreIpcApplication() {
    }

    public static void main(String[] args) {
        int exitCode = run(args, System.in, System.out, System.err);
        if (exitCode != 0) {
            System.exit(exitCode);
        }
    }

    public static int run(
            String[] args,
            InputStream input,
            OutputStream output,
            OutputStream errorOutput
    ) {
        Arguments arguments;
        try {
            arguments = Arguments.parse(args);
        } catch (IllegalArgumentException exception) {
            writeStatic(errorOutput, "IPC_ARGUMENTS_INVALID\n" + USAGE);
            return 2;
        }
        if (arguments.help()) {
            writeStatic(output, USAGE);
            return 0;
        }

        try (SqliteActionJournal journal = new SqliteActionJournal(arguments.journalPath())) {
            IpcProcessor processor = new IpcProcessor(
                    new PilotModelRoutingPolicy(arguments.externalModelsEnabled()),
                    journal
            );
            LimitedLineReader reader = new LimitedLineReader(
                    new InputStreamReader(input, StandardCharsets.UTF_8),
                    IpcProcessor.MAX_FRAME_CHARACTERS
            );
            PrintWriter writer = new PrintWriter(
                    new OutputStreamWriter(output, StandardCharsets.UTF_8),
                    true
            );
            while (true) {
                String line;
                try {
                    line = reader.readLine();
                } catch (LimitedLineReader.LineTooLongException exception) {
                    writer.println(processor.messageTooLargeResponse());
                    continue;
                }
                if (line == null) {
                    break;
                }
                writer.println(processor.process(line));
            }
            return writer.checkError() ? 4 : 0;
        } catch (SQLException | IOException exception) {
            writeStatic(errorOutput, "IPC_RUNTIME_UNAVAILABLE\n");
            return 3;
        }
    }

    private static void writeStatic(OutputStream output, String text) {
        PrintWriter writer = new PrintWriter(
                new OutputStreamWriter(output, StandardCharsets.UTF_8),
                true
        );
        writer.print(text);
        writer.flush();
    }

    private record Arguments(
            Path journalPath,
            boolean externalModelsEnabled,
            boolean help
    ) {
        private static Arguments parse(String[] args) {
            if (args == null) {
                throw new IllegalArgumentException("args must not be null");
            }
            Path journalPath = null;
            boolean externalModelsEnabled = false;
            boolean help = false;
            for (int index = 0; index < args.length; index++) {
                switch (args[index]) {
                    case "--journal" -> {
                        if (journalPath != null || index + 1 >= args.length) {
                            throw new IllegalArgumentException("Invalid journal argument");
                        }
                        journalPath = Path.of(args[++index]);
                    }
                    case "--external-models-enabled" ->
                            externalModelsEnabled = true;
                    case "--help", "-h" -> help = true;
                    default -> throw new IllegalArgumentException("Unknown argument");
                }
            }
            if (!help && journalPath == null) {
                throw new IllegalArgumentException("--journal is required");
            }
            return new Arguments(journalPath, externalModelsEnabled, help);
        }
    }
}
