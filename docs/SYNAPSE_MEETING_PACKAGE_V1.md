# Пакет встречи eXpress (Синапс) 1.0

## Текущая граница возможности

RnD Workbench умеет безопасно импортировать локальный экспорт встречи eXpress,
который в корпоративном контуре называется «Синапс», из каталога или ZIP-файла.
Значение `source_system = synapse` сохраняется как legacy alias контракта. Это
**fallback-импорт и локальная реализация**, а не подключение к API eXpress:

- `package_import_available = true`;
- `corporate_api_connected = false`;
- `real_integration = false`;
- `write_back_available = false`;
- `live_connector_available = false`;
- `reason_code = CORPORATE_API_NOT_CONNECTED`.

Импорт не выполняет сетевых запросов и не меняет данные во внешних системах.
Создаваемые предложения имеют режим `draft_only`.

## Состав пакета

Корень каталога или ZIP должен содержать `manifest.json` и перечисленные в нём
файлы. Минимальный пример:

```text
meeting-export/
├── manifest.json
├── transcript.txt
├── description.md
└── attachments/
    └── plan.md
```

```json
{
  "schema_version": "1.0",
  "source_system": "synapse",
  "import_mode": "LOCAL_PACKAGE_IMPORT",
  "package_id": "synapse-meeting-42",
  "meeting": {
    "title": "Статус пилота",
    "occurred_at": "2026-08-31T10:00:00+03:00",
    "participants": ["Анна", "Иван"],
    "organizer": "Анна",
    "classification": "confidential"
  },
  "transcript": {
    "path": "transcript.txt",
    "media_type": "text/plain",
    "sha256": "<64 lowercase hex>"
  },
  "description": {
    "path": "description.md",
    "media_type": "text/markdown",
    "sha256": "<64 lowercase hex>"
  },
  "attachments": [
    {
      "path": "attachments/plan.md",
      "title": "План запуска",
      "media_type": "text/markdown",
      "sha256": "<64 lowercase hex>"
    }
  ],
  "metadata": {
    "project": "pilot",
    "duration_seconds": 1800
  }
}
```

Транскрипт и описание обязательны и должны быть UTF-8. Допустимо до 32
вложений; общий объём читаемых частей ограничен 250 МБ. ZIP дополнительно
ограничен 128 записями и 512 UTF-8 байтами на имя записи. Абсолютные пути,
`..`, обратные слеши, дубли путей, зашифрованные ZIP, повторяющиеся JSON-поля и
secret-like ключи metadata отклоняются до записи контекста.

## Результат импорта

После проверки приложение последовательными локальными транзакциями создаёт:

1. основной source с транскриптом и meeting analysis;
2. связанный source описания;
3. по одному source на вложение;
4. отношения `synapse.description` и `synapse.attachment`;
5. provenance с `package_id`, fingerprint, ролью, исходным относительным путём,
   media type, SHA-256 и размером;
6. локальный audit event со статусом `local_mock`.

Для неподдерживаемого или повреждённого бинарного вложения сохраняется
metadata-only источник, а не выдуманный распознанный текст. Чтение
`word/document.xml` внутри DOCX ограничено 8 МБ, а извлечённый текст — двумя
миллионами символов. При ошибке созданные строки, meeting items, отношения и
управляемые копии файлов откатываются best-effort; исходный пакет никогда не
удаляется.

Одинаковые `package_id` и fingerprint дают `already_imported`. Повтор того же
`package_id` с другим fingerprint отклоняется как конфликт. Профиль
`synapse-meeting-package-fingerprint-v2` одинаково рассчитывается Python- и
Java-контрактами. Он включает название и время встречи, организатора,
классификацию, дедуплицированное и отсортированное множество участников,
metadata, названия и содержимое частей. Cursor/watermark — транспортный receipt
и в identity fingerprint не входит.

Импорт не является одной большой ACID-транзакцией: граф и управляемые файлы
создаются восстановимыми этапами. Идентичность пакета защищает долговечный
SQLite receipt с уникальным ключом `(source_system, external_id)`, fingerprint,
workspace, UUID fencing token и 15-минутным lease. Поэтому два обычных процесса
не создают два графа: второй получает `import_in_progress`; просроченный claim
может быть захвачен только CAS-обновлением того же receipt. Lease обновляется
между этапами, а complete фиксируется только действующим token.

Если процесс остановился до финального complete receipt/audit marker, следующая
доставка того же fingerprint проверяет граф, удаляет только принадлежащие
незавершённой попытке sources и fingerprint-prefixed orphan files, затем
импортирует пакет заново. Если complete уже есть, но граф или файлы повреждены,
автоматического удаления нет: возвращается требование явного repair, чтобы не
потерять пользовательские изменения статусов встречи. Теоретическая гонка
остаётся только если один неразрывный этап зависнет дольше lease; строгий
exactly-once для такого случая потребует единой транзакционной модели файлов и
графа и пока не заявляется.

## Детерминированные последующие возможности

Сразу после импорта backend возвращает полный обогащённый контекст:

- подготовку повестки следующей встречи;
- решения;
- поручения и обязательства;
- риски;
- открытые вопросы;
- локальные черновики последующих действий.

Каждый извлечённый элемент содержит provenance: source ID, диапазон символов и
цитату из транскрипта. Порядок повестки и идентификаторы предложений стабилен
при повторном чтении того же пакета.

Описание и текстовые вложения возвращаются отдельно в `supporting_context` как
ограниченные snippets с source ID, типом связи, классификацией и metadata части.
Они явно помечены как дополнительный контекст и не смешиваются с решениями,
поручениями, рисками и вопросами, извлечёнными из транскрипта. Тот же раздел с
provenance добавляется в briefing следующей встречи.

Команда desktop-backend:

```json
{"command":"import_synapse_package","path":"/path/to/export.zip","workspace_id":"<optional>"}
```

Успех публикуется событием `synapse_package_imported`; в `result` уже находятся
анализ, повестка, предложения, provenance и честный capability gate. Выбор файла
или каталога выполняет нативная оболочка; Python-команда не открывает UI сама.

## Контракт будущего live connector eXpress

Экспорт может опционально нести checkpoint:

```json
{
  "connector_checkpoint": {
    "delivery_mode": "POLLING",
    "cursor": "opaque-cursor-42",
    "watermark": "2026-08-31T07:00:00Z"
  }
}
```

Допустимы `POLLING` и `WEBHOOK`; обязателен хотя бы `cursor` или `watermark`.
Сейчас checkpoint только валидируется и сохраняется в provenance. Он не входит
в content fingerprint: один пакет может быть повторно доставлен с новым
checkpoint. Его наличие выставляет `checkpoint_accepted = true`, но **не**
делает `live_connector_available` истинным.

Основной будущий live path — eXpress Recordings Bot/BotX. Локальный package
import останется fallback. Когда корпоративный connector будет реализован, его
алгоритм должен быть таким:

1. polling получает пакет после последнего подтверждённого cursor/watermark;
   webhook принимает тот же versioned package contract;
2. до разбора выполняется dedup по `package_id` и fingerprint;
3. пакет валидируется и полностью сохраняется локально;
4. только после durable success polling продвигает checkpoint, а webhook
   подтверждает доставку;
5. тот же ID с тем же fingerprint возвращает идемпотентный success, с другим —
   conflict;
6. retry после ошибки не продвигает checkpoint и не подтверждает webhook.

Watched-folder источник и корпоративный API в версии 1.0 не реализованы. Их
нельзя показывать как активную интеграцию до появления connector, авторизации,
наблюдаемости и эксплуатационных тестов.
