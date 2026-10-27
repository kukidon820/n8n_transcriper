# n8n-transcriper

Локальный асинхронный CLI-конвертер: читает экспорт workflow n8n в JSON, проверяет его и создаёт отдельный Python-проект. Это командная программа, а не n8n-сервер и не постоянно работающий сервис.

## Содержание

- [Возможности](#возможности)
- [Поддержка узлов](#поддержка-узлов)
- [Установка](#установка)
- [Быстрый старт](#быстрый-старт)
- [Команды CLI](#команды-cli)
- [Что создаёт convert](#что-создаёт-convert)
- [Настройка и секреты](#настройка-и-секреты)
- [Ошибки, прогресс и логи](#ошибки-прогресс-и-логи)
- [Архитектура проекта](#архитектура-проекта)
- [Разработка и проверки](#разработка-и-проверки)
- [Документация по папкам](#документация-по-папкам)
- [Ограничения](#ограничения)

## Возможности

- Чтение workflow из локального JSON-файла или доступного HTTP(S) URL.
- Проверка структуры и поддерживаемых типов/версий узлов.
- Просмотр краткой сводки workflow без вывода содержимого параметров узлов.
- Генерация отдельного проекта с backend <code>python-core</code> или <code>langgraph</code>.
- Асинхронный runtime, ветвление, обратные связи и контролируемое выполнение циклов.
- Адаптеры HTTP на <code>aiohttp</code>, PostgreSQL на SQLAlchemy async + <code>asyncpg</code>, SMTP и файловые узлы.
- Явная диагностика ограничений и файл <code>MANUAL_CHANGES.md</code> для ручной доработки.

Список проверенных узлов, версий и параметров поддерживается отдельно в [матрице конвертеров](src/n8n_to_python/converters/README.md). Наличие узла в n8n само по себе не означает полной совместимости.

## Поддержка узлов

Конвертер использует зарегистрированные версии <code>node.type</code> и <code>typeVersion</code>. Параметры и операции проверяются отдельно. При неподдерживаемой возможности команда показывает диагностику; результат конвертации перечисляет необходимые ручные действия. Безопасный workflow не считается полностью переносимым, пока эти сообщения не проверены.

Ключевые группы узлов включают triggers, Python Code, Edit Fields, IF, Switch, Loop Over Items, Merge, HTTP Request, Webhook, Compression, Extract from File, Data Table, Postgres и Email. LangGraph добавляет ограниченный вариант AI Agent; Python Core AI Agent не исполняет. JavaScript Code намеренно не включён.

Подробности и исключения для конкретных версий находятся в [матрице поддержки](src/n8n_to_python/converters/README.md).

## Установка

Нужны Python 3.12 или новее и [uv](https://docs.astral.sh/uv/). В корне репозитория:

~~~powershell
uv sync --locked
uv run n8n-to-python --help
~~~

Команда <code>uv sync --locked</code> устанавливает зависимости по lock-файлу. CLI запускается из окружения проекта; отдельно активировать виртуальное окружение не требуется.

## Быстрый старт

Пусть экспорт n8n сохранён как <code>workflow.json</code> в текущей папке:

~~~powershell
uv run n8n-to-python validate .\workflow.json
uv run n8n-to-python inspect .\workflow.json
uv run n8n-to-python convert .\workflow.json --output .\generated-workflow
~~~

Перед запуском результата изучите созданный <code>MANUAL_CHANGES.md</code> и настройте подключения. Затем перейдите в папку результата:

~~~powershell
Set-Location .\generated-workflow
uv sync
uv run run-workflow
~~~

Относительные пути к входному файлу считаются от текущей папки терминала. Можно передать абсолютный путь. GitHub URL с сегментом <code>/blob/</code> автоматически преобразуется в raw URL; удалённый адрес должен отдавать JSON и быть доступен по сети.

## Команды CLI

Посмотреть подсказку:

~~~powershell
uv run n8n-to-python --help
uv run n8n-to-python convert --help
~~~

| Команда | Для чего нужна | Аргументы и параметры |
| --- | --- | --- |
| <code>validate &lt;source&gt;</code> | Загружает JSON, проверяет модель workflow, граф и известную поддержку узлов. | <code>source</code> — локальный путь или HTTP(S) URL. |
| <code>validate &lt;source&gt; --strict</code> | Выполняет те же проверки и завершает команду ошибкой при предупреждениях. | <code>--strict</code> делает предупреждения о поддержке блокирующими. |
| <code>inspect &lt;source&gt;</code> | Показывает имя workflow, количество узлов/связей, triggers, типы, версии и диагностику. | Не выводит полные параметры узлов, чтобы не раскрывать большие настройки и возможные секреты. |
| <code>convert &lt;source&gt; --output &lt;directory&gt;</code> | Проверяет workflow и генерирует отдельный проект. | <code>--output</code> или <code>-o</code> — обязательная папка результата. Она должна отсутствовать или быть пустой. |

Дополнительные параметры <code>convert</code>:

| Параметр | Значение |
| --- | --- |
| <code>--backend python-core</code> | Собственный async runtime. Это backend по умолчанию. |
| <code>--backend langgraph</code> | LangGraph runtime и ограниченная поддержка AI Agent. |
| <code>--project-name NAME</code> | Имя проекта в метаданных Python и README результата. По умолчанию берётся имя workflow. |
| <code>--strict</code> | Не создавать проект, если обнаружены предупреждения. |

Примеры:

~~~powershell
# Локальная проверка; предупреждения отображаются, но не блокируют
uv run n8n-to-python validate .\workflow.json

# Строгая проверка поддерживаемости
uv run n8n-to-python validate .\workflow.json --strict

# Сводка по удалённому JSON
uv run n8n-to-python inspect https://example.org/exports/workflow.json

# Генерация Python Core проекта
uv run n8n-to-python convert .\workflow.json -o .\generated-core --backend python-core

# Генерация LangGraph проекта с явным именем
uv run n8n-to-python convert .\workflow.json -o .\generated-agent --backend langgraph --project-name orders-flow
~~~

CLI не перезаписывает непустую выходную папку. Ошибки чтения, Pydantic-валидации, проверки и записи завершают команду с ненулевым кодом возврата.

## Что создаёт convert

Сгенерированный проект — самостоятельный Python package. Его основные файлы:

~~~text
generated-workflow/
├── pyproject.toml
├── README.md
├── MANUAL_CHANGES.md
└── src/
    ├── generated_workflow/
    │   ├── main.py
    │   ├── services.py
    │   ├── manual_changes.py
    │   └── workflow.json
    └── n8n_to_python/
        ├── converters/
        └── runtime/
~~~

<code>workflow.json</code> хранит проверенный экспорт. <code>main.py</code> создаёт runtime registry, загружает workflow через Pydantic и запускает выбранный движок. <code>services.py</code> собирает внешние подключения. <code>manual_changes.py</code> отмечает действия, которые разработчик должен закрыть после реализации. Runtime и нужные адаптеры поставляются вместе с результатом.

В проекте есть точка запуска <code>run-workflow</code>. При успехе конечные items печатаются в stdout как JSON. Один запуск программы выполняет один запуск workflow. Это не фоновый daemon и не веб-сервер.

Отдельная папка pytest-тестов для каждого generated workflow сейчас не создаётся. Тесты репозитория проверяют конвертеры, runtime и запуск сгенерированных проектов на fixtures.

## Настройка и секреты

Секреты не следует помещать в экспорт workflow, исходники или generated <code>workflow.json</code>. Используйте переменные окружения процесса:

| Переменная | Назначение |
| --- | --- |
| <code>DATABASE_URL</code> | Подключение PostgreSQL для узлов Postgres и Data Table; используется SQLAlchemy async с драйвером <code>asyncpg</code>. |
| <code>SMTP_HOST</code>, <code>SMTP_PORT</code> | SMTP-сервер и порт для Email. Порт по умолчанию — 587. |
| <code>SMTP_USERNAME</code>, <code>SMTP_PASSWORD</code> | SMTP-аутентификация. |
| <code>SMTP_STARTTLS</code>, <code>SMTP_USE_TLS</code> | Настройки TLS для SMTP-адаптера. |
| <code>DEEPSEEK_API_KEY</code> | Ключ для поддержанного AI Agent в LangGraph backend. |
| <code>DEEPSEEK_BASE_URL</code>, <code>DEEPSEEK_MODEL</code> | Необязательные адрес API и имя модели; используются значения по умолчанию, указанные в generated README. |

Сейчас проект читает переменные напрямую из окружения. <code>pydantic-settings</code> и <code>python-dotenv</code> не подключены, поэтому файл <code>.env</code> автоматически не загружается. Для PowerShell значения можно задать в текущем окне:

~~~powershell
$env:DATABASE_URL = "postgresql+asyncpg://USER:PASSWORD@HOST:5432/DB_NAME"
$env:SMTP_HOST = "smtp.example.org"
$env:SMTP_PORT = "587"
~~~

Не помещайте реальные значения в коммиты и не выводите их в логи. Сгенерированный README и <code>MANUAL_CHANGES.md</code> описывают нужные конкретному workflow подключения.

## Ошибки, прогресс и логи

Во время <code>validate</code> и <code>convert</code> CLI кратко сообщает в stderr о начале заметных этапов. По завершении команда показывает итог; при ошибке печатается короткое русское объяснение без Pydantic-дампа и внутренних кодов. Ошибка не превращается в пустой результат: команда завершается с кодом 1. Для технических подробностей используйте:

- сообщения CLI и код диагностики;
- <code>logs/n8n-to-python.log</code> относительно папки, из которой вызван CLI;
- список ручных действий в generated <code>MANUAL_CHANGES.md</code>.

CLI показывает в консоли короткие русские сообщения; уровни логов тоже локализованы. Подробности исключений сохраняются в ротируемом файле журнала. В сгенерированном приложении та же система логирования пока не настраивается: успешные terminal outputs идут в stdout, необработанное исключение — в stderr с ненулевым завершением процесса.

Узлы с неподдержанными действиями или настройками перечисляются как ручные изменения. Generated runtime блокирует запуск, пока эти действия не реализованы и не закрыты в <code>src/generated_workflow/manual_changes.py</code>. Ошибка узла во время исполнения останавливает workflow; runtime добавляет имя узла и сохраняет исходное исключение.

## Архитектура проекта

Конвертер использует асинхронное ООП, Pydantic v2 и четыре основных слоя. <code>converters</code> и <code>runtime</code> — вспомогательные пакеты, а не дополнительные слои.

~~~text
src/n8n_to_python/
├── cli/             Typer-команды, аргументы, прогресс и форматирование вывода
├── application/     сценарии загрузки, проверки, инспекции и конвертации
├── domain/          Pydantic-модели workflow, узлов и connections
├── infrastructure/  файловые/URL источники, запись проекта, DB и SMTP
├── converters/      стратегии backend и адаптеры отдельных типов/версий узлов
└── runtime/         async engines, runtime items, план исполнения и ошибки
~~~

Направление зависимостей: CLI вызывает Application; Application использует Domain и порты; Infrastructure реализует внешние адаптеры. Domain не должен импортировать CLI или Infrastructure. Подробнее: [руководство по архитектуре пакета](src/n8n_to_python/README.md).

## Разработка и проверки

Из корня проекта:

~~~powershell
uv sync --locked
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
~~~

Большинство тестов используют локальные fixtures и локальные HTTP/SMTP endpoints. Сетевой тест загрузки Git URL требует интернета. Тесты с реальным PostgreSQL запускаются при заданной переменной <code>N8N_TEST_POSTGRES_URL</code>; без неё эта группа пропускается. Подробности описаны в [руководстве по тестам](tests/README.md).

При добавлении поддержки нового узла обновляйте адаптер, точную версию реестра, тесты, таблицу поддержки и пример ограничений. Краткий процесс описан в [руководстве converters](src/n8n_to_python/converters/README.md).

## Документация по папкам

| Документ | Что в нём |
| --- | --- |
| [CLI](src/n8n_to_python/cli/README.md) | Команды, параметры, коды завершения, вывод и логи. |
| [Application](src/n8n_to_python/application/README.md) | Сценарии и границы use case-ов. |
| [Application models](src/n8n_to_python/application/models/README.md) | Структурированные результаты и диагностики. |
| [Application ports](src/n8n_to_python/application/ports/README.md) | Контракты внешних зависимостей. |
| [Application services](src/n8n_to_python/application/services/README.md) | Координация загрузки, проверки и конвертации. |
| [Domain](src/n8n_to_python/domain/README.md) | Модель workflow и правила связей. |
| [Infrastructure](src/n8n_to_python/infrastructure/README.md) | Адаптеры, конфигурация и lifecycle соединений. |
| [Converters](src/n8n_to_python/converters/README.md) | Матрица узлов, версии, expressions и ограничения. |
| [Runtime](src/n8n_to_python/runtime/README.md) | Передача items, граф, циклы, backends и ошибки. |
| [Tests](tests/README.md) | Структура тестов, fixtures и интеграционные условия. |

## Ограничения

Конвертация не является полной эмуляцией n8n. Произвольный JavaScript и полный runtime n8n expressions не исполняются. HTTP credentials, multipart и pagination, неподдержанные операции БД/Email и другие ограничения следует сверять с матрицей и диагностикой. Email поддерживает отправку файлов из binary-полей, но SMTP credentials задаются вручную через переменные окружения. Сначала проверяйте workflow командой <code>validate</code> и изучайте файлы generated проекта перед запуском.

## Даты в expressions и Email-вложения

Expression evaluator поддерживает $now.toFormat() для форматирования текущей даты. Например, ={{ $now.toFormat("yyyy-LL-dd") }} вернёт дату в формате год-месяц-день. Поддерживаемые токены и часовой пояс описаны в [матрице конвертеров](src/n8n_to_python/converters/README.md).

Узел Email принимает экспортные параметры resource, operation, emailFormat, options.fileAttachments и options.appendAttribution. fileAttachments указывает одно или несколько имён binary-полей через запятую; ZIP из узла Compression отправляется как MIME-вложение. Для SMTP пользователь задаёт параметры окружения в сгенерированном проекте. JavaScript Code в демонстрационном workflow остаётся неподдержанным и требует ручной замены на Python Code.
