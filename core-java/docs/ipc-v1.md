# RnD Workbench Core IPC 1.0

Java core запускается дочерним процессом и читает UTF-8 JSONL из `stdin`. На
каждую непустую входную строку он синхронно пишет ровно одну UTF-8 JSON-строку в
`stdout` и сразу её сбрасывает. Диагностические сообщения процесса не содержат
входные данные, тексты исключений или пути; `stdout` зарезервирован для
протокола.

```bash
gradle run --args="--journal /safe/local/path/action-journal.sqlite"
```

Максимальный размер одного frame — 65 536 UTF-16 characters на Java boundary;
вложенность JSON ограничена 20 уровнями, строки внутри JSON — 32 768 symbols.
Повторяющиеся поля, неизвестные поля, числовые enum, пропущенные обязательные
поля и trailing JSON отклоняются. Единственная поддерживаемая версия — `1.0`.

JSON Schema поставляются в runtime JAR:

- `schema/ipc-request-v1.schema.json`;
- `schema/ipc-response-v1.schema.json`.

## Конверт

Запрос:

```json
{"version":"1.0","type":"health.check","correlationId":"desktop-start-1","payload":{}}
```

Успешный ответ:

```json
{"correlationId":"desktop-start-1","ok":true,"payload":{"protocolVersion":"1.0","status":"ready"},"type":"health.status","version":"1.0"}
```

Ошибка всегда имеет безопасный фиксированный текст и не отражает исходный
frame:

```json
{"correlationId":"unavailable","error":{"code":"INVALID_JSON","message":"The frame is not valid strict JSON."},"ok":false,"type":"error","version":"1.0"}
```

`correlationId` возвращается без изменений только после успешной проверки
конверта. Для синтаксически неверного JSON или неверного конверта используется
`unavailable`.

## Выбор модели

`route.decide` принимает только классификацию, предпочтение и признаки
доступности/разрешений. Prompt, расшифровка голоса и документы в Java не
передаются.

```json
{"version":"1.0","type":"route.decide","correlationId":"turn-42","payload":{"classification":"CORPORATE_INTERNAL","preference":"AUTO","availableRoutes":{"local":true,"corporate":true,"external":false},"corporateScopeAuthorized":true,"explicitExternalConsent":false}}
```

```json
{"correlationId":"turn-42","ok":true,"payload":{"localFallbackBeforeFirstOutput":false,"reason":"LOCAL_SELECTED","route":"LOCAL","status":"SELECTED"},"type":"route.decision","version":"1.0"}
```

Внешний route всё равно проверяет `PUBLIC`, explicit consent и process flag
`--external-models-enabled`. Флаг по умолчанию выключен.

## План локального импорта встречи eXpress (Синапс)

`meeting.package.plan` проверяет платформенно-независимую часть manifest и
возвращает детерминированный fingerprint и фиксированный набор последующих
возможностей. Legacy alias `sourceSystem` остаётся `synapse`. Команда не читает
файлы и не заявляет подключение к eXpress —
контент безопасно проверяет и импортирует Python backend.

```json
{"version":"1.0","type":"meeting.package.plan","correlationId":"meeting-42","payload":{"schemaVersion":"1.0","sourceSystem":"synapse","importMode":"LOCAL_PACKAGE_IMPORT","packageId":"synapse-meeting-42","title":"Статус пилота","occurredAt":"2026-08-31T10:00:00+03:00","organizer":"Анна","classification":"confidential","participants":["Анна","Иван"],"parts":[{"role":"TRANSCRIPT","relativePath":"transcript.txt","title":"Транскрипт","mediaType":"text/plain","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","sizeBytes":1200},{"role":"DESCRIPTION","relativePath":"description.md","title":"Описание","mediaType":"text/markdown","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","sizeBytes":320}],"metadata":{"project":"pilot"}}}
```

Успешный `meeting.package.plan.result` всегда сообщает:

- `packageImportAvailable: true`;
- `fingerprintProfile: synapse-meeting-package-fingerprint-v2`;
- `corporateApiConnected: false`;
- `realIntegration: false`;
- `writeBackAvailable: false`;
- `liveConnectorAvailable: false`;
- `reasonCode: CORPORATE_API_NOT_CONNECTED`.

Опциональный `connectorCheckpoint` принимает `deliveryMode` `POLLING` или
`WEBHOOK` и хотя бы одно непрозрачное значение `cursor`/`watermark`. Checkpoint
не входит в content fingerprint; его наличие означает только
`checkpointAccepted: true`, а не работающий live connector. Полный формат
пакета, provenance и семантика будущей доставки описаны в
[`docs/SYNAPSE_MEETING_PACKAGE_V1.md`](../../docs/SYNAPSE_MEETING_PACKAGE_V1.md).
Основной будущий live connector должен использовать eXpress Recordings Bot/BotX;
локальный пакет остаётся fallback.

## Persistent action journal

Перед внешним side effect connector вызывает `action.claim`:

```json
{"version":"1.0","type":"action.claim","correlationId":"action-42-attempt-1","payload":{"idempotencyKey":"jira:RND-42:version-7","requestFingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}
```

Возможные `disposition`:

- `CLAIMED` — только этот caller получает UUID `claimToken` и может выполнять
  действие;
- `REPLAY` — действие уже завершено, возвращается сохранённый безопасный
  результат;
- `IN_PROGRESS` — claim уже существует, повторно выполнять действие нельзя;
- `CONFLICT` — тот же key использован с другим fingerprint.

После side effect владелец claim сохраняет результат:

```json
{"version":"1.0","type":"action.complete","correlationId":"action-42-attempt-1","payload":{"idempotencyKey":"jira:RND-42:version-7","requestFingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","claimToken":"00000000-0000-4000-8000-000000000001","outcome":"SUCCESS","resultCode":"ISSUE.UPDATED","externalReference":"RND-42","completedAt":"2026-08-31T13:00:00Z"}}
```

`RECORDED` означает первую атомарную запись результата, `REPLAY` — повтор той же
записи. Неизвестный claim возвращает `NOT_CLAIMED`, неверный token/fingerprint
или изменённый результат — `CONFLICT`.

SQLite хранит только key, SHA-256 fingerprint, correlation ID, UUID claim,
timestamps, outcome, безопасный машинный `resultCode` и опциональную короткую
внешнюю ссылку. В таблице нет payload, prompt, response body, аудио, документов
или credentials.

Claim и result атомарны на уровне локальной SQLite-транзакции. После crash row в
состоянии `CLAIMED` сохраняется и после restart возвращает `IN_PROGRESS`, поэтому
процесс не запустит эффект второй раз вслепую. Это намеренная fail-closed
семантика. Журнал не может дать distributed exactly-once для произвольного
внешнего API: connector должен сверить состояние с Jira/Kaiten/Confluence и
затем завершить или администрируемо исправить зависший claim. Автоматического
lease expiry нет, поскольку оно создало бы риск дублирующего side effect.

## Запуск и выходные коды

- `0` — EOF обработан штатно;
- `2` — неверные аргументы запуска;
- `3` — недоступен runtime/journal/stdin;
- `4` — ошибка записи `stdout`.

Обязательный аргумент `--journal` задаёт отдельный SQLite-файл профиля. Windows
Python backend уже запускает процесс для metadata-only `health.check` и
`route.decide`; Electron получает только безопасную диагностику. Swift/macOS и
connector action adapters пока не подключены и составляют следующие
вертикальные инкременты.
