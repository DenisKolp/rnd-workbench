from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .store import utc_now


PILOT_SLO_METRICS = (
    "listen_ready_seconds",
    "transcript_ready_seconds",
    "first_audio_seconds",
    "barge_in_stop_seconds",
    "input_clipping_ratio",
    "output_clipping_ratio",
)


@dataclass(frozen=True, slots=True)
class PilotPreflightInputs:
    platform: str
    storage_ready: bool
    llm_ready: bool
    stt_ready: bool
    tts_ready: bool
    microphone_verified: bool
    java_policy_ready: bool
    action_journal_ready: bool
    connected_systems: tuple[str, ...] = ()
    manual_meeting_import_ready: bool = True
    distribution_verified: bool = False
    metrics_summary: Mapping[str, Any] | None = None


def _check(
    check_id: str,
    title: str,
    status: str,
    detail: str,
    action: str = "",
) -> dict[str, str]:
    if status not in {"pass", "warn", "block", "unverified"}:
        raise ValueError("Некорректный статус preflight")
    return {
        "id": check_id,
        "title": title,
        "status": status,
        "detail": detail,
        "action": action,
    }


def _voice_slo_check(summary: Mapping[str, Any] | None) -> dict[str, str]:
    metrics = summary.get("metrics") if isinstance(summary, Mapping) else None
    if not isinstance(metrics, Mapping):
        metrics = {}
    statuses: list[str] = []
    for metric_name in PILOT_SLO_METRICS:
        metric = metrics.get(metric_name)
        slo = metric.get("slo") if isinstance(metric, Mapping) else None
        status = slo.get("status") if isinstance(slo, Mapping) else None
        statuses.append(str(status or "missing"))
    if "fail" in statuses:
        return _check(
            "voice_slo",
            "Качество голосового контура",
            "block",
            "Хотя бы один измеренный голосовой SLO не выполнен.",
            "Откройте сводку качества и устраните метрику со статусом fail.",
        )
    passed = statuses.count("pass")
    if passed == len(PILOT_SLO_METRICS):
        return _check(
            "voice_slo",
            "Качество голосового контура",
            "pass",
            "Все обязательные локальные SLO подтверждены минимум пятью измерениями.",
        )
    return _check(
        "voice_slo",
        "Качество голосового контура",
        "unverified",
        f"Подтверждено метрик: {passed} из {len(PILOT_SLO_METRICS)}.",
        "Выполните минимум пять реплик, включая перебивание, на реальном устройстве.",
    )


def build_pilot_preflight(inputs: PilotPreflightInputs) -> dict[str, Any]:
    """Build a deterministic, content-free readiness report for one device."""

    if inputs.platform not in {"macos", "windows", "linux"}:
        raise ValueError("Неизвестная платформа preflight")
    checks = [
        _check(
            "storage",
            "Локальное хранилище",
            "pass" if inputs.storage_ready else "block",
            (
                "SQLite доступна, схема и быстрый integrity-check корректны."
                if inputs.storage_ready
                else "Локальная база не прошла integrity-check."
            ),
            "Восстановите доступ к каталогу приложения и повторите проверку."
            if not inputs.storage_ready
            else "",
        ),
        _check(
            "llm",
            "Языковая модель",
            "pass" if inputs.llm_ready else "block",
            (
                "Выбранный локальный или корпоративный маршрут готов."
                if inputs.llm_ready
                else "Нет готового маршрута для текстового ответа."
            ),
            "Настройте локальную или корпоративную модель."
            if not inputs.llm_ready
            else "",
        ),
        _check(
            "stt",
            "Распознавание речи",
            "pass" if inputs.stt_ready else "block",
            "Локальный STT готов." if inputs.stt_ready else "Локальный STT не готов.",
            "Установите или укажите проверенные веса Whisper."
            if not inputs.stt_ready
            else "",
        ),
        _check(
            "tts",
            "Озвучивание ответа",
            "pass" if inputs.tts_ready else "block",
            "Локальный TTS готов." if inputs.tts_ready else "Локальный TTS не готов.",
            "Запустите и проверьте OmniVoice-Fast."
            if not inputs.tts_ready
            else "",
        ),
        _check(
            "microphone",
            "Микрофон и аудиоустройство",
            "pass" if inputs.microphone_verified else "unverified",
            (
                "Захват звука подтверждён текущим приложением."
                if inputs.microphone_verified
                else "Устройство ещё не проверено фактическим запуском записи."
            ),
            "Запустите голосовой режим и произнесите тестовую реплику."
            if not inputs.microphone_verified
            else "",
        ),
        _voice_slo_check(inputs.metrics_summary),
        _check(
            "java_policy",
            "Политика маршрутизации Java 21",
            "pass" if inputs.java_policy_ready else "warn",
            (
                "Общая metadata-only политика маршрутизации готова."
                if inputs.java_policy_ready
                else "Активна резервная Python-политика; расхождения блокируют запрос."
            ),
            "Проверьте bundled Java 21 companion."
            if not inputs.java_policy_ready
            else "",
        ),
        _check(
            "action_journal",
            "Защита внешних действий",
            "pass" if inputs.action_journal_ready else "warn",
            (
                "Java-журнал идемпотентности готов."
                if inputs.action_journal_ready
                else "Production-запись во внешние системы будет заблокирована."
            ),
            "Восстановите Java action journal перед подключением исполнителей."
            if not inputs.action_journal_ready
            else "",
        ),
        _check(
            "meeting_import",
            "Ручной импорт встреч eXpress",
            "pass" if inputs.manual_meeting_import_ready else "block",
            (
                "Доступен локальный импорт аудио, транскрипта или ZIP с контекстом."
                if inputs.manual_meeting_import_ready
                else "Нет проверяемого входа для первой вертикали встреч."
            ),
        ),
        _check(
            "corporate_connectors",
            "Production-интеграции",
            "pass" if inputs.connected_systems else "warn",
            (
                f"Подключено систем: {len(inputs.connected_systems)}."
                if inputs.connected_systems
                else "Корпоративные API-исполнители ещё не подключены."
            ),
            "Нужны тестовые endpoint, OAuth/SSO и владельцы API."
            if not inputs.connected_systems
            else "",
        ),
        _check(
            "distribution",
            "Подпись и поставка",
            "pass" if inputs.distribution_verified else "warn",
            (
                "Подпись и чистая установка подтверждены."
                if inputs.distribution_verified
                else "Текущая сборка не подтверждена как production-поставка."
            ),
            "Проведите подпись, чистую установку и device smoke-test."
            if not inputs.distribution_verified
            else "",
        ),
    ]
    counts = {
        status: sum(check["status"] == status for check in checks)
        for status in ("pass", "warn", "block", "unverified")
    }
    overall = (
        "blocked"
        if counts["block"]
        else "limited"
        if counts["warn"] or counts["unverified"]
        else "ready"
    )
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "platform": inputs.platform,
        "overall": overall,
        "counts": counts,
        "checks": checks,
        "content_transmitted": False,
    }


__all__ = [
    "PILOT_SLO_METRICS",
    "PilotPreflightInputs",
    "build_pilot_preflight",
]
