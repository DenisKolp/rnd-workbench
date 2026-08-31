package com.rndworkbench.core.integration;

import com.rndworkbench.core.autonomy.ActionKind;
import com.rndworkbench.core.data.DataClassification;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

/** Validation and canonical hashing for bridge-safe integration actions. */
public final class IntegrationActionContract {
    private static final Set<String> SECRET_KEYS = Set.of(
            "password",
            "passwd",
            "secret",
            "clientsecret",
            "apikey",
            "apitoken",
            "accesstoken",
            "refreshtoken",
            "authorization",
            "bearertoken",
            "privatekey"
    );

    private IntegrationActionContract() {
    }

    public static Map<String, Object> freezeParameters(Map<String, ?> parameters) {
        Objects.requireNonNull(parameters, "parameters");
        @SuppressWarnings("unchecked")
        Map<String, Object> frozen = (Map<String, Object>) freezeValue(
                parameters,
                "parameters"
        );
        return frozen;
    }

    public static String fingerprint(
            String connector,
            String operation,
            IntegrationIntent intent,
            ActionKind actionKind,
            DataClassification classification,
            Map<String, ?> parameters
    ) {
        Objects.requireNonNull(intent, "intent");
        Objects.requireNonNull(actionKind, "actionKind");
        Objects.requireNonNull(classification, "classification");
        Map<String, Object> frozen = freezeParameters(parameters);
        String canonical = "connector=" + connector
                + "\noperation=" + operation
                + "\nintent=" + intent.name()
                + "\nactionKind=" + actionKind.name()
                + "\nclassification=" + classification.name()
                + "\nparameters=" + canonicalValue(frozen);
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(
                    canonical.getBytes(StandardCharsets.UTF_8)
            );
            return java.util.HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("Java runtime does not provide SHA-256", exception);
        }
    }

    private static Object freezeValue(Object value, String path) {
        if (value == null || value instanceof String || value instanceof Boolean) {
            return value;
        }
        if (value instanceof Byte
                || value instanceof Short
                || value instanceof Integer
                || value instanceof Long
                || value instanceof Float
                || value instanceof Double
                || value instanceof BigInteger
                || value instanceof BigDecimal) {
            Number number = (Number) value;
            normalizedNumber(number, path);
            return number;
        }
        if (value instanceof Map<?, ?> map) {
            List<Map.Entry<?, ?>> entries = new ArrayList<>(map.entrySet());
            entries.sort(Comparator.comparing(entry -> String.valueOf(entry.getKey())));
            LinkedHashMap<String, Object> copy = new LinkedHashMap<>();
            for (Map.Entry<?, ?> entry : entries) {
                if (!(entry.getKey() instanceof String key) || key.isBlank()) {
                    throw new IllegalArgumentException(path + " contains a blank or non-string key");
                }
                rejectSecretKey(key, path);
                copy.put(key, freezeValue(entry.getValue(), path + "." + key));
            }
            return Collections.unmodifiableMap(copy);
        }
        if (value instanceof List<?> list) {
            List<Object> copy = new ArrayList<>(list.size());
            for (int index = 0; index < list.size(); index++) {
                copy.add(freezeValue(list.get(index), path + "[" + index + "]"));
            }
            return Collections.unmodifiableList(copy);
        }
        throw new IllegalArgumentException(
                path + " contains a non-JSON value: " + value.getClass().getName()
        );
    }

    private static void rejectSecretKey(String key, String path) {
        String normalized = key.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]", "");
        if (SECRET_KEYS.contains(normalized)
                || normalized.endsWith("password")
                || normalized.endsWith("secret")
                || normalized.endsWith("apikey")) {
            throw new IllegalArgumentException(
                    path + " contains forbidden secret field: " + key
            );
        }
    }

    private static String canonicalValue(Object value) {
        if (value == null) {
            return "null";
        }
        if (value instanceof String text) {
            return "s" + text.length() + ":" + text;
        }
        if (value instanceof Boolean flag) {
            return flag ? "true" : "false";
        }
        if (value instanceof Number number) {
            return "n:" + normalizedNumber(number, "parameters");
        }
        if (value instanceof List<?> list) {
            StringBuilder result = new StringBuilder("[");
            for (Object item : list) {
                result.append(canonicalValue(item)).append(';');
            }
            return result.append(']').toString();
        }
        if (value instanceof Map<?, ?> map) {
            StringBuilder result = new StringBuilder("{");
            map.entrySet().stream()
                    .sorted(Comparator.comparing(entry -> (String) entry.getKey()))
                    .forEach(entry -> result
                            .append(canonicalValue(entry.getKey()))
                            .append('=')
                            .append(canonicalValue(entry.getValue()))
                            .append(';'));
            return result.append('}').toString();
        }
        throw new IllegalArgumentException("Unsupported canonical value: " + value);
    }

    private static String normalizedNumber(Number number, String path) {
        if ((number instanceof Double doubleValue && !Double.isFinite(doubleValue))
                || (number instanceof Float floatValue && !Float.isFinite(floatValue))) {
            throw new IllegalArgumentException(path + " contains a non-finite number");
        }
        try {
            BigDecimal decimal = new BigDecimal(number.toString()).stripTrailingZeros();
            return decimal.compareTo(BigDecimal.ZERO) == 0
                    ? "0"
                    : decimal.toPlainString();
        } catch (NumberFormatException exception) {
            throw new IllegalArgumentException(path + " contains an invalid number", exception);
        }
    }
}
