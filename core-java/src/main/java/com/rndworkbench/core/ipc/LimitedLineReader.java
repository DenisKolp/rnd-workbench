package com.rndworkbench.core.ipc;

import java.io.IOException;
import java.io.Reader;
import java.util.Objects;

final class LimitedLineReader {
    private final Reader reader;
    private final int maximumCharacters;

    LimitedLineReader(Reader reader, int maximumCharacters) {
        this.reader = Objects.requireNonNull(reader, "reader");
        if (maximumCharacters < 1) {
            throw new IllegalArgumentException("maximumCharacters must be positive");
        }
        this.maximumCharacters = maximumCharacters;
    }

    String readLine() throws IOException, LineTooLongException {
        StringBuilder line = new StringBuilder(Math.min(maximumCharacters, 1024));
        boolean readAnything = false;
        boolean tooLong = false;
        while (true) {
            int next = reader.read();
            if (next == -1 || next == '\n') {
                if (!readAnything && next == -1) {
                    return null;
                }
                break;
            }
            readAnything = true;
            if (line.length() < maximumCharacters) {
                line.append((char) next);
            } else {
                tooLong = true;
            }
        }
        if (tooLong) {
            throw new LineTooLongException();
        }
        int length = line.length();
        if (length > 0 && line.charAt(length - 1) == '\r') {
            line.setLength(length - 1);
        }
        return line.toString();
    }

    static final class LineTooLongException extends Exception {
        private static final long serialVersionUID = 1L;
    }
}
