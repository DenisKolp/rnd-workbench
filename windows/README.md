# RnD Workbench — Windows Electron pilot

Версия Windows-компонента — **0.1.0-pilot**; она входит в общий продуктовый
milestone **0.9.9**, но не обозначает готовую Windows-поставку.

Это минимальная честная Windows-ветка для пилота на 30 сотрудниках. Клиент
реализован **только на Electron**. Компактный голосовой виджет и полноразмерное
рабочее окно — два состояния одного `BrowserWindow`; переход не создаёт второй
экземпляр интерфейса или backend.

Windows-ветка содержит текстовый рабочий цикл, локальное хранение задач,
маршрутизацию между локальной и корпоративной языковой моделью и полный
capability-gated voice vertical в исходниках. Без заранее подготовленных
Faster-Whisper weights и loopback OmniVoice-Fast server Python-мост честно
сообщает `voice_available=false`; Windows-сборка и акустический тракт пока не
проверены на реальном Windows-устройстве.

## Матрица готовности

| Возможность | Статус Windows pilot | Что реально работает |
|---|---:|---|
| Electron compact ↔ full | Готово в исходниках | Один `BrowserWindow` меняет bounds, always-on-top и layout |
| Защита от второго экземпляра | Готово в исходниках | `requestSingleInstanceLock()` активирует существующее окно |
| Текстовый чат и потоковый ответ | Готово в исходниках | JSONL frontend ↔ core, OpenAI-compatible SSE |
| Локальная модель | Готово в исходниках | Loopback endpoint `localhost` / `127.0.0.1`, например Ollama или LM Studio |
| Корпоративная модель | Готово в исходниках | Удалённый OpenAI-compatible HTTPS endpoint; ключ только в памяти процесса |
| Рабочие пространства и задачи | Частично | SQLite-история и задачи работают; полный macOS-набор экранов не перенесён |
| Политика данных | Готово для чата | Local допускает локальный контекст; corporate ограничен классификацией и ручным контекстом |
| Java 21 route gate | Проверено локально и в CI | Перед LLM Python передаёт в companion только классификацию, предпочтение и доступность маршрутов; несовпадение блокирует запрос, сбой включает видимый Python fallback |
| Java 21 autonomy gate | Проверено локально | Перед интеграцией companion получает только категорию действия; решение сверяется с Python, несовпадение блокирует вызов, fallback виден в интерфейсе |
| Java 21 action journal | Проверено локально и в CI | Согласованные production-действия получают metadata-only claim; повтор и конфликт блокируются, прерванный результат сверяется без повторного вызова |
| Голосовой ввод, STT | Готово в исходниках | Electron WebAudio → mono PCM16/16 кГц, adaptive VAD/pre-roll и Faster-Whisper; нужен Windows hardware QA |
| Глобальная диктовка | Готово в исходниках | Удержание F8 записывает речь, отпускание вставляет текст через UI Automation/SendInput; secure fields отклоняются, нужен Windows hardware QA |
| TTS, единый голос, перебивание | Готово в исходниках | Один короткий OmniVoice-Fast запрос с фиксированными profile/seed, PCM stream, limiter, 12-мс fade и barge-in; локальная агрегированная диагностика готова, акустические SLO на реальном устройстве не подтверждены |
| Импорт встреч eXpress | Готово в исходниках | Локальный Faster-Whisper обрабатывает аудио; доступны готовый транскрипт и быстрый ZIP. Read-only корпоративный intake скрыт до admin-конфигурации и считается подключённым только после успешной проверки endpoint |
| Jira / Kaiten / Confluence / почта / календарь | Не подключено | Нужны корпоративные API, OAuth/SSO и тестовые стенды |
| Voice-ready portable QA artifact | Проверено в CI | Frozen backend импортирует Faster-Whisper, CTranslate2 и Tokenizers, запускает bundled Java route gate и action journal probe; artifact хранится семь дней |
| Preflight устройства | Готово в исходниках | 12 content-free проверок показывают реальные блокеры, предупреждения, crash-free после 20 завершений и неподтверждённые SLO; результат можно обновить в полном окне |
| Быстрый старт | Готово в исходниках | Один content-free следующий шаг ведёт через первый результат, голос, импорт встречи и сводку; блок скрыт в свёрнутой диагностике |
| Подписанный установщик | Не проверено | Windows CI создаёт unsigned portable artifact; веса Whisper, OmniVoice server, подпись и hardware QA не входят в эту проверку |

Статусы «в исходниках» означают, что код и автоматические контрактные тесты
готовы в репозитории. Это не заменяет запуск, подпись и UX-проверку на реальных
Windows 11 устройствах пилотной группы.

Production renderer дополнительно проверен в compact 410×420 и full 1120×760:
страница не прокручивается, все видимые кнопки умещают подписи, длинные задачи
переносятся на две строки, переход назван «Развернуть»/«Виджет», а начальный
контур настроек согласован с локальными подсказками. Компактная голосовая
подсказка не перегружает экран, а техническая диагностика пилота свёрнута по
умолчанию.

## Архитектура

```text
Electron renderer (без Node.js)
        │ безопасный preload / IPC
Electron main process — единственный BrowserWindow
        │ JSON Lines через stdin/stdout
Windows core service
        ├─ Python bridge: chat + STT/OmniVoice adapters
        └─ Java 21 companion: route/autonomy gates + action journal через IPC 1.0
```

Renderer не получает Node.js API и не открывает внешние страницы. В main
process включены `contextIsolation`, `sandbox`, `nodeIntegration=false` и CSP.
Диагностика `stderr` core не копируется в интерфейс, чтобы случайно не показать
локальные пути или тело ответа провайдера.

JSONL остаётся целевой границей. Переменная
`RND_WORKBENCH_CORE_EXECUTABLE` позволяет в разработке явно подставить другой
исполняемый Python/ML core. В packaged-сборке Electron запускает
`resources/backend/rnd-workbench-backend.exe`, а тот — bundled Java 21 companion
из `resources/java-core`. В Java уходят только enum/boolean metadata политики;
prompt, транскрипты, документы, ответы модели и ключи остаются в Python/native
runtime. При несовпадении политик LLM не вызывается, а при недоступности Java
интерфейс явно показывает резервную встроенную Python-политику. Persistent
action journal подключён к общему integration hub: production write получает
claim до вызова, результат фиксируется после него, а перезапуск выполняет только
сверку. Реальные корпоративные API-адаптеры пока не подключены, поэтому UI не
выдаёт подготовленный запрос за выполненное действие.

## Запуск из репозитория на Windows

Требуются Node.js LTS и Python 3.11+. Для полной portable-сборки также нужны JDK
21 с `jlink` и Gradle 9.7.1. Windows-окружение намеренно не устанавливает
root-пакет с Apple-only MLX-зависимостями. Базовые зависимости сборки находятся
в `windows/requirements-build.txt`, voice runtime — в
`windows/requirements-voice.txt`.

Electron зафиксирован на версии 44.0.0, electron-builder — 26.15.3; committed
`package-lock.json` проверен `npm audit` без известных уязвимостей.

```powershell
cd windows\electron
npm install
npm start
```

Или запустите `windows\run-rnd-workbench.cmd` после `npm install`.

Чтобы capability голоса стала доступна, до запуска задайте путь к заранее
размещённой модели Faster-Whisper и адрес уже работающего локального
OmniVoice-Fast server:

```powershell
$env:RND_WORKBENCH_WINDOWS_WHISPER_MODEL = "C:\models\whisper-large-v3-turbo"
$env:RND_WORKBENCH_WINDOWS_OMNIVOICE_URL = "http://127.0.0.1:8080"
$env:RND_WORKBENCH_WINDOWS_STT_DEVICE = "cpu" # либо cuda в подготовленном окружении
```

Приложение не скачивает веса и не запускает неподписанный server скрытно. В
диагностике отдельно видны готовность capture, STT и TTS. Полный ответ остаётся
в чате; голосом воспроизводится одна законченная реплика до 220 символов. Полное
окно показывает обезличенную 14-дневную сводку задержек и сигнала. В свёрнутом
блоке рядом доступны 28-дневные агрегаты активных дней, завершённых запросов,
диктовки и meeting-сценариев, время первого результата, оценка 1–5 и crash-free
доля завершённых сессий. JSON-отчёт не содержит запросы, транскрипты, документы,
сырые даты или идентификаторы.
Рядом доступен динамический preflight устройства; статичная декларация
готовности не используется.

Корпоративный сервис нормализованных пакетов встреч настраивается deployment-
администратором, а не пользователем интерфейса:

```powershell
$env:RND_WORKBENCH_EXPRESS_INTAKE_URL = "https://express-intake.corp.example/bridge"
$env:RND_WORKBENCH_EXPRESS_INTAKE_TOKEN = "<из защищённого launcher/vault>"
```

Токен не сохраняется. Кнопка «Получить из eXpress» появляется только при полной
конфигурации; cursor продвигается после успешного импорта всей страницы.

В настройках выберите один из маршрутов:

- Local: `http://127.0.0.1:11434/v1`, идентификатор модели из Ollama/LM Studio,
  API-ключ не нужен.
- Corporate: HTTPS endpoint, модель и выданный пользователю API-ключ. Ключ
  потребуется ввести заново после перезапуска.

## Portable-сборка

Скрипт не устанавливает PyInstaller автоматически. Используйте отдельное
build-окружение, где Python, PyInstaller, Node.js и npm уже установлены:

```powershell
powershell -ExecutionPolicy Bypass -File windows\build.ps1
```

Voice-вариант собирается только в окружении с закреплёнными зависимостями из
`windows/requirements-voice.txt`:

```powershell
python -m pip install -r windows\requirements-voice.txt
powershell -ExecutionPolicy Bypass -File windows\build.ps1 -WithVoice
```

Путь к весам Whisper и URL OmniVoice не требуются во время сборки: это
deployment-настройки, которые проверяются при запуске capability голоса.

Для воспроизводимости сборки скрипт использует `npm ci` и committed
`package-lock.json`, а не переразрешает зависимости перед упаковкой.

Результат создаётся в `windows\dist\electron`. GitHub Actions выполняет этот шаг
на Windows runner, запускает packaged backend smoke-test и сохраняет unsigned
voice-ready QA artifact на семь дней. Frozen backend включает библиотеки
Faster-Whisper/CTranslate2/Tokenizers и проходит их runtime-import probe. Рядом
упаковываются Java libraries, ограниченный JDK 21 `jlink` image и лицензионные
уведомления; packaged smoke-test делает реальный health/route probe. Artifact не
включает веса Whisper или OmniVoice-Fast server и не считается пилотной
поставкой до подписи, smoke-test на чистой Windows 11 и hardware/acoustic QA.

## Следующий обязательный этап голоса

Source-контракт уже реализует `capability`, `state`, `metric`,
`dictation_ready`, `assistant_delta`, `assistant_end`, `speech_error`, PCM audio
events и `session_stopped`. Автотесты проверяют порядок и границы сообщений, а
цифровая диагностика считает peak/clipping. CI уже собирает voice-ready portable
artifact и запускает frozen dependency probe, но это ещё не аппаратная
валидация: перед пилотом нужны измерения end-of-speech → transcript, first audio,
speaker consistency, реального cancel → mute и barge-in на нескольких моделях
Windows-ноутбуков и гарнитур. Portable artifact также должен пройти подпись и
smoke-test на чистой Windows 10/11.
