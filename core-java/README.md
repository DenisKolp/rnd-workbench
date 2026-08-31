# RnD Workbench Core (Java 21)

Инкрементальный платформенно-независимый слой доменных политик RnD Workbench.
Модуль не заменяет существующее приложение и не переносит ML inference в JVM.
Версия пока не подключённого desktop-оболочками компонента — `0.1.0-SNAPSHOT`; он
разрабатывается в рамках продуктового milestone `0.9.1` и не является
отдельным релизом.

## Что уже зафиксировано кодом

- классификация данных и local-first выбор локальной/корпоративной модели;
- запрет внешних моделей в политике пилота по умолчанию;
- отдельные проверки классификации, области доступа и явного согласия;
- четыре уровня автономности: без подтверждения, с уведомлением и отменой,
  с предпросмотром, с явным подтверждением;
- vendor-neutral контракт интеграционного действия;
- стабильный SHA-256 fingerprint, ключ идемпотентности, correlation ID и номер
  попытки;
- глубокая неизменяемая копия параметров и запрет передачи секретов в payload;
- исполняемый локальный процесс с версионированным UTF-8 JSONL stdio-контрактом
  `1.0`, строгой валидацией полей и детерминированным JSON;
- команды `health.check`, `route.decide`, `meeting.package.plan`, `action.claim`
  и `action.complete`;
- платформенно-независимый контракт локального пакета встречи eXpress
  (legacy alias `synapse`) с
  fingerprint, provenance checkpoint и честным запретом заявлять live API;
- persistent SQLite WAL-журнал с атомарным claim по idempotency key,
  fingerprint-конфликтами, ownership token и безопасным metadata-only
  результатом;
- JSON Schema запросов и ответов в `src/main/resources/schema` и описание
  протокола в [`docs/ipc-v1.md`](docs/ipc-v1.md).

Таблицы политик соответствуют
[`docs/PILOT_PRODUCT_CONTRACT.md`](../docs/PILOT_PRODUCT_CONTRACT.md). Архитектурная
граница и порядок внедрения описаны в
[`ADR-0001`](../docs/ADR-0001-CROSS_PLATFORM_JAVA_CORE.md).

## Граница модуля

```text
macOS Swift shell / Windows Electron shell
                    |
           versioned local contract
                    |
              Java 21 core
        policies + action envelopes
                    |
         Python inference bridge
      Whisper / LLM runtime / OmniVoice
```

Java core не владеет микрофоном, аудиобуферами, UI, ML-моделями и секретами
коннекторов. Поэтому latency-critical voice path не получает лишнюю
сериализацию между аудиочанками. Core принимает решение до запуска модели и
оформляет внешнее действие до его передачи исполнителю.

## Проверка

Нужны JDK 21 и Gradle 8.5+:

```bash
cd core-java
gradle test
```

Сборка настроена с `-Xlint:all -Werror`. Все 42 JUnit-теста прошли локально на
JDK 21.0.12 и Gradle 9.7.1; Windows CI должен повторить эту проверку перед
включением модуля в дистрибутив.

## Следующий инкремент

1. Добавить golden contract tests схемы `1.0` в Python- и TypeScript-адаптеры.
2. Подключить обе оболочки к Java-процессу, сохранив текущий Python path как
   fallback на время миграции.
3. Первым перевести read-only model routing, не затрагивая latency-critical
   голосовой тракт.
4. Добавить Windows CI и `jlink` runtime image для воспроизводимой поставки.
5. Реализовать reconciliation зависших `CLAIMED` действий до подключения
   production-коннекторов Jira/Kaiten/Confluence/почты/календаря.
