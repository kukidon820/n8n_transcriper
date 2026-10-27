# Конвертеры узлов n8n

`NodeConverterRegistry` находит реализацию по точной паре `node.type` и `typeVersion`, валидирует параметры через Pydantic и строит runtime-реестр. `PythonCoreGenerator` и `LangGraphGenerator` собирают отдельные проекты в памяти; запись результатов через команды CLI относится к этапу 9.

## Версии n8n и `typeVersion`

`typeVersion` — версия реализации **конкретного узла**, сохранённая в этом узле workflow. Это не версия установленного приложения n8n. Например, `HTTP Request` с `typeVersion: 4.2` и `HTTP Request` с `typeVersion: 4.5` — разные входные контракты для конвертера. n8n оставляет старую версию в уже сохранённом workflow после появления новой; новые workflow обычно получают актуальную версию узла. Поэтому версия n8n сама по себе не меняет `typeVersion` существующего узла.

Ниже перечислены версии узлов, которые объявлены в текущем upstream-коде n8n на **2026-10-26**, версии, найденные в JSON-файлах проекта, и точные версии, для которых сейчас зарегистрирован наш адаптер. Это история версий узлов, а не соответствие «typeVersion → выпуск приложения n8n»: локальные workflow не содержат надёжной информации, из какого выпуска n8n был сделан экспорт. Списки upstream могут меняться; при обновлении n8n их надо сверять с исходниками/документацией и отдельно проверять поведение адаптера.

| Узел (`node.type`) | Версии в upstream n8n | Версии в fixtures проекта | Адаптер проекта сейчас |
| --- | --- | --- | --- |
| [Manual Trigger](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/ManualTrigger/ManualTrigger.node.ts) (`n8n-nodes-base.manualTrigger`) | 1 | 1 | 1 |
| [Schedule Trigger](https://docs.n8n.io/integrations/builtin/deprecated-nodes/#versioned-nodes) (`n8n-nodes-base.scheduleTrigger`) | 1, 1.1, 1.2, 1.3, 1.4 | 1.3 | 1.3 — формирует payload одного события на вызов runtime; периодический запуск пока не входит в адаптер |
| [Webhook](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Webhook/Webhook.node.ts) (`n8n-nodes-base.webhook`) | 1, 1.1, 2, 2.1, 2.2 | 2.1 | 1, 1.1, 2, 2.1, 2.2 |
| [Code](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Code/Code.node.ts) (`n8n-nodes-base.code`) | 1, 2 | 2 | 2 — только Python Native |
| [Edit Fields / Set](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Set/Set.node.ts) (`n8n-nodes-base.set`) | 1, 2, 3, 3.1, 3.2, 3.3, 3.4, 3.5 | 3.4 | 3, 3.1, 3.2, 3.3, 3.4, 3.5 |
| [Data Table](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/DataTable/DataTable.node.ts) (`n8n-nodes-base.dataTable`) | 1, 1.1 | 1, 1.1 | 1, 1.1 |
| [HTTP Request](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/HttpRequest/HttpRequest.node.ts) (`n8n-nodes-base.httpRequest`) | 1, 2, 3, 4, 4.1, 4.2, 4.3, 4.4, 4.5 | 4.2, 4.5 | 4, 4.1, 4.2, 4.3, 4.4, 4.5 |
| [Compression](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Compression/Compression.node.ts) (`n8n-nodes-base.compression`) | 1, 1.1 | 1.1 | 1.1 |
| [Extract from File](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Files/ExtractFromFile/ExtractFromFile.node.ts) (`n8n-nodes-base.extractFromFile`) | 1, 1.1, 1.2 | 1.2 | 1.2 |
| [Loop Over Items](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/SplitInBatches/SplitInBatches.node.ts) (`n8n-nodes-base.splitInBatches`) | 1, 2, 3 | 3 | 3 |
| [IF](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/If/If.node.ts) (`n8n-nodes-base.if`) | 1, 2, 2.1, 2.2, 2.3, 2.4 | 2.2 | 2, 2.1, 2.2, 2.3, 2.4 |
| [Switch](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Switch/Switch.node.ts) (`n8n-nodes-base.switch`) | 1, 2, 3, 3.1, 3.2, 3.3, 3.4, 3.5 | 3.2 | 3, 3.1, 3.2, 3.3, 3.4, 3.5 |
| [Send Email](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/EmailSend/EmailSend.node.ts) (`n8n-nodes-base.emailSend`) | 1, 2, 2.1 | 2.1 | 2, 2.1 |
| [Postgres](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Postgres/Postgres.node.ts) (`n8n-nodes-base.postgres`) | 1, 2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8 | 2.6 | 2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8 |
| [Merge](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Merge/Merge.node.ts) (`n8n-nodes-base.merge`) | 1, 2, 2.1, 3, 3.1, 3.2 | 3.2 | 3.2 — только `mode=append` |
| [AI Agent](https://github.com/n8n-io/n8n/blob/master/packages/@n8n/nodes-langchain/nodes/agents/Agent/Agent.node.ts) (`@n8n/n8n-nodes-langchain.agent`) | 1, 1.1–1.9, 2, 2.1–2.3, 3, 3.1 | 2.2, 3.1 | LangGraph: 2.2 только с DeepSeek Chat Model v1, Calculator Tool v1 и Simple Memory v1.3 |

Списки версий сверены с [официальным описанием версионирования узлов](https://docs.n8n.io/integrations/creating-nodes/build/reference/node-versioning/), [таблицей versioned nodes](https://docs.n8n.io/integrations/builtin/deprecated-nodes/#versioned-nodes) и исходниками n8n: [HTTP Request](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/HttpRequest/HttpRequest.node.ts), [Set / Edit Fields](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Set/Set.node.ts), [IF](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/If/If.node.ts), [Switch](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Switch/Switch.node.ts), [Postgres](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Postgres/Postgres.node.ts), [Webhook](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/Webhook/Webhook.node.ts), [AI Agent](https://github.com/n8n-io/n8n/blob/master/packages/@n8n/nodes-langchain/nodes/agents/Agent/Agent.node.ts). Документация n8n и текущие исходники иногда обновляются не одновременно; для списка конкретного узла ориентируйся на его upstream source и проверяй его перед выпуском.

Пары `HTTP Request v4.2`, `Data Table v1`, `Schedule Trigger v1.3` и `Merge v3.2` имеют интеграционные сценарии. AI Agent v2.2 выполняется только в LangGraph backend и только с подключёнными DeepSeek Chat Model v1, Calculator Tool v1 и Simple Memory v1.3; v3.1 и другие AI-модели, tools и memory types явно не поддерживаются. Python Core сообщает об AI Agent как о неподдержанном узле и требует ручной доработки. Добавляй версии отдельными точными записями и тестируй каждую пару `node.type` + `typeVersion`; нельзя автоматически принимать версии по условию вроде `typeVersion >= 4`.

В корневом примере n8n_public_api_python_csv_zip_email.json AI Agent v2.2 связан с DeepSeek Chat Model v1, Calculator Tool v1 и Simple Memory v1.3. В Email поддерживаются ZIP-вложения; JavaScript Code остаётся ручным изменением и поэтому блокирует полный запуск этого workflow до замены узла на Python Code.

## Поддержанные подмножества

| Узел | `type` | Версия | Что выполняется |
| --- | --- | ---: | --- |
| Manual Trigger | `n8n-nodes-base.manualTrigger` | 1 | Один начальный `{}` item; в runtime можно передать начальные items явно. |
| Schedule Trigger | `n8n-nodes-base.scheduleTrigger` | 1.3 | Валидирует `rule.interval` и формирует поля одного события с указанной временной зоной при вызове runtime. Сам периодический таймер/cron-loop не запускается этим адаптером. |
| Webhook | `n8n-nodes-base.webhook` | 1, 1.1, 2, 2.1, 2.2 | Callable trigger получает `initial_items`; `render_webhook_entrypoint()` строит async entry point. Сам конвертер HTTP-сервер не запускает. Поддержан `responseMode=onReceived`. |
| Code (Python) | `n8n-nodes-base.code` | 2 | `language=pythonNative` или `python`, `pythonCode`/`code`, режимы `runOnceForAllItems` и `runOnceForEachItem`, `_items`/`_item`, словарь или список словарей на выходе. JavaScript Code исключён из целевого объёма по просьбе пользователя. |
| Edit Fields / Set | `n8n-nodes-base.set` | 3, 3.1, 3.2, 3.3, 3.4, 3.5 | Режим `manual`, типизированные назначения, `$json`/`$binary`, dot/bracket доступ, интерполяция, арифметика, сравнения, логика, `??`, тернарный оператор и whitelist функций. Поддержаны `includeOtherFields` и dot notation. |
| IF | `n8n-nodes-base.if` | 2, 2.1, 2.2, 2.3, 2.4 | Типизированные числовые/строковые/boolean сравнения и `and`/`or`. |
| Switch | `n8n-nodes-base.switch` | 3, 3.1, 3.2, 3.3, 3.4, 3.5 | Режим `rules`, первый или все совпавшие выходы, fallback `none`, `extra` или индекс. |
| Loop Over Items | `n8n-nodes-base.splitInBatches` | 3.0 | Пакеты `batchSize`, выходы done/loop, обратные связи в графе; `options.reset` должен быть `false`. |
| HTTP Request | `n8n-nodes-base.httpRequest` | 4, 4.1, 4.2, 4.3, 4.4, 4.5 | GET/HEAD/OPTIONS/POST/PUT/PATCH/DELETE, URL, query, headers, JSON body, вложенные expressions, timeout; только `aiohttp`. Response Format: `json`, `text`, `file`, `autodetect`; поддержан `fullResponse`, `neverError`, JSON object/array/scalar/null, текст и бинарные файлы. |
| Compression | `n8n-nodes-base.compression` | 1.1 | Сжимает и распаковывает один ZIP или GZIP binary property, без записи файлов на диск. |
| Extract from File | `n8n-nodes-base.extractFromFile` | 1.2 | CSV с UTF-8/UTF-8-SIG/Latin-1 и заголовком или без него; JSON object/list; извлечение текста из PDF через `pypdf`. |
| Data Table | `n8n-nodes-base.dataTable` | 1, 1.1 | Row: `get`, `insert`, `update`, `upsert`, `deleteRows`, `rowExists`, `rowNotExists`. Нужен runtime service `data_table_store`; реализация SQLAlchemy хранит строки в SQL JSON-колонке. |
| Postgres | `n8n-nodes-base.postgres` | 2, 2.1–2.8 | `executeQuery` через внедрённый порт `PostgresClient`; `$1`, `$2` связываются как параметры SQLAlchemy. Production-адаптер использует SQLAlchemy async с `postgresql+asyncpg`. |
| Send Email | n8n-nodes-base.emailSend | 2, 2.1 | SMTP text/HTML, To/From/Subject, CC/BCC/Reply-To, resource=email, operation=send, emailFormat=text/html/both, binary-вложения из options.fileAttachments (в том числе ZIP), appendAttribution. Настройки SMTP задаются через env vars. |
| Merge | `n8n-nodes-base.merge` | 3.2 | Только `mode=append`; ожидает все настроенные main inputs, объединяет items по индексу входа. Если ожидаемый вход не пришёл — runtime завершает workflow ошибкой. |
| AI Agent | `@n8n/n8n-nodes-langchain.agent` | 2.2, только LangGraph | Одна DeepSeek Chat Model v1, не более одного Calculator Tool v1 и не более одного Simple Memory v1.3. Поддержаны `promptType=define`, `text`, `options.systemMessage`, `options.maxIterations` и выражения, которые понимает общий evaluator. API key берётся из `DEEPSEEK_API_KEY`; HTTP вызывается через `aiohttp`. |

Перечисленные версии проходят один и тот же адаптер только там, где upstream объявляет общий формат узла; тесты запускают базовый сценарий для каждой зарегистрированной версии. Это не означает поддержку всех комбинаций параметров n8n. Другие версии, операции и параметры явно отклоняются или показываются как предупреждение. JavaScript Code не входит в требования проекта.

## Форма данных и выходы узлов

n8n передаёт данные как ноль или больше items. Runtime хранит у каждого item поле `json` и необязательный набор именованных `binary` файлов; выход узла может содержать несколько items и несколько выходных портов. Свойство `json` может быть объектом, массивом или скалярным JSON-значением, которое вернул HTTP/JSON-узел. Узлы, которым нужны поля (`Edit Fields`, Data Table и некоторые интеграции), ожидают объект и завершаются понятной ошибкой, если получили scalar. Binary payload сейчас хранится inline как base64 вместе с `mimeType`, `fileName`, `fileExtension` и `fileSize`, что удобно для переносимого generated project, но ограничивает размер.

| Узел/группа | Что получает следующий узел |
| --- | --- |
| Manual/Schedule/Webhook triggers | Один или несколько начальных items; webhook entry point принимает переданные JSON/binary items, сам конвертер сервер не запускает. |
| Edit Fields и Python Code | Изменённые JSON items; Edit Fields сохраняет binary, Python Code принимает/возвращает словари с `json` и опциональным `binary`. |
| IF, Switch, Loop, Merge | Исходные items с их JSON/binary; адаптеры маршрутизируют или объединяют их по выходным портам. |
| Compression | Один binary item при ZIP/GZIP compress; при распаковке ZIP создаёт по binary item на каждый файл. |
| Extract from File | CSV — по item на строку; JSON — по item на верхнеуровневый элемент (или один item для скалярного JSON); PDF — текст по документу либо странице. `keepSource` управляет сохранением исходного binary. |
| Data Table и Postgres | По item на каждую найденную/возвращённую строку; при SQL без rows Postgres возвращает item с `rowCount`. Исходный binary сохраняется, если он был на входе. |
| Send Email | JSON receipt на входной item: accepted, rejected, response, envelope, messageId. Вложения читаются из binary item и отправляются как MIME-файлы; ошибка SMTP останавливает workflow. |
| AI Agent (LangGraph) | Изменённый входной item с текстом ответа в `json.output`; бинарные данные сохраняются. AI model/tool/memory connections сами по себе не становятся `main` items. |

### HTTP Request: варианты ответа

| Response Format | Результат item |
| --- | --- |
| `json` | JSON object — один item; JSON array — отдельный item на каждый элемент; scalar JSON сохраняется как значение `json`; `null` нормализуется в `{}`. Некорректный JSON завершает узел ошибкой. |
| `text` | Строка в `outputPropertyName` (по умолчанию `data`); если сервер вернул JSON MIME type, объект/array сериализуется в JSON-текст. Пустой ответ остаётся пустой строкой. |
| `file` | Байты ответа в `binary[outputPropertyName]`; входной JSON и прежние binary fields сохраняются, кроме `fullResponse`, где JSON заменяется метаданными статуса/заголовков. |
| `autodetect` | `application/json` → JSON; известные image/audio/video/document/archive MIME-типы → binary file; прочие типы → text. |
| `fullResponse` | Для JSON содержит `body`, `headers`, `statusCode`, `statusMessage`; для text `outputPropertyName` содержит body плюс метаданные; для file body находится только в binary, а JSON содержит метаданные. |

`neverError=true` позволяет вернуть не-2xx response и прочитать status/body; по умолчанию HTTP status error завершает workflow. Ответ ограничен 50 MiB в этом runtime. Входящие массивы response items сохраняют связь со своим входным item, даже если один ответ раскрылся в несколько items. Этот список описывает выходные формы, а не все возможности HTTP Request: credentials, multipart/raw body, pagination, proxy и некоторые другие параметры отклоняются и записываются в `MANUAL_CHANGES.md`.

## Как читаются JSON expressions

Expressions остаются строками из n8n JSON, например:

```json
{
  "url": "={{ $json.api.host }}",
  "jsonBody": {
    "userId": "={{ $json.user.id }}",
    "message": "Привет, {{ $json.user.name }}"
  }
}
```

Конвертер рекурсивно проходит по словарям и спискам параметров. Строку `={{ ... }}` разбирает собственный токенизатор и Pratt parser, строит небольшое дерево выражения и вычисляет его относительно текущего item. Полное выражение возвращает исходный тип (`user.id` останется числом); встраивание `{{ ... }}` в обычный текст даёт строку. Произвольный `eval()`/`exec()` для expressions не используется.

Сейчас разрешены $json, $binary, $itemIndex, $input.item/first()/last()/all() и ссылки на предыдущие узлы с доступом к .json/.binary; поддерживаются цепочки .all().first()/last(). Реализованы доступ к JSON, литералы, арифметика, сравнения, логика, тернарные выражения и ограниченный набор безопасных функций. $now.toFormat() поддерживает токены yyyy, yy, LL, L, dd, d, HH, H, hh, h, mm, m, ss, s, SSS, ZZ; пример: ={{ $now.toFormat("yyyy-LL-dd") }}. Часовой пояс берётся из runtime service timezone, по умолчанию UTC. Произвольный JavaScript не исполняется, неизвестные методы и токены завершаются понятной ошибкой.

Не поддерживаются $env, $vars, optional chaining, regex, JMESPath, Math, произвольные методы Luxon и JavaScript. Исключение — ограниченный $now.toFormat() с токенами из списка выше. Выражения исполняются только безопасным parser-ом без eval()/exec(); неподдерживаемое выражение отмечается для ручной доработки.

Для HTTP Request query, headers, URL и JSON body используют один evaluator. Значения внутри JSON body тоже вычисляются рекурсивно, а не остаются исходными строками.

## Справочник expressions

В корне проекта добавлен [справочник n8n Expressions: 533 примера и 12 сквозных кейсов](../../../N8N_EXPRESSIONS_СПРАВОЧНИК_500_ПРИМЕРОВ.md). В нём собраны примеры JavaScript-подобного синтаксиса n8n, `$json`, `$input`, ссылок на другие узлы и item linking, переменных окружения, методов строк и массивов, regex, `Math`, дат/Luxon, JMESPath, HTTP, AI, SQL/Postgres, Email и binary data.

Справочник — учебная подборка, а не обещание совместимости конвертера. Реализовано только ограниченное подмножество, перечисленное выше. В частности, не реализованы `$env`/`$vars`/`$now`, optional chaining `?.`, regex, полный набор `Math`, методы массивов `map`/`filter`/`reduce`, Luxon, JMESPath, IIFE и произвольный JavaScript. Такие формы не следует считать поддержанными только потому, что для них есть пример в справочнике; неподдержанное выражение должно завершаться явной ошибкой.

## Интеграции и переменные окружения

- Для Postgres runtime получает порт `services={"postgres_client": client}`. `infrastructure.database.create_postgres_client_from_env()` создаёт `SQLAlchemyPostgresClient` на SQLAlchemy AsyncEngine из `DATABASE_URL` (PostgreSQL + `asyncpg`). При завершении работы вызови `await client.close()`, чтобы освободить пул соединений. URL и пароль не выводятся в логи.
- Для Data Table передаётся `services={"data_table_store": store}`; рабочая реализация — `SQLAlchemyDataTableStore(engine)`. Внутренний формат строк хранится в общей JSON-таблице, а не создаёт SQL-таблицу для каждого n8n Data Table.
- Для Email runtime получает порт `services={"email_sender": sender}`. `infrastructure.email_sender.create_email_sender_from_env()` создаёт SMTP-адаптер, который читает `SMTP_HOST`, `SMTP_PORT` (по умолчанию 587), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS` и `SMTP_USE_TLS`. Секреты не берутся из workflow JSON и не логируются.
- Python Code запускается только при явном выполнении runtime и выполняется обычным `exec` в worker thread. Это **не sandbox** и не воспроизводит ограничения n8n task runner. Запускай только доверенный workflow. Автоматическая конвертация/загрузка JSON не исполняет этот код.

## Ограничения

- Binary представлен inline base64 в `WorkflowItem.binary`. ZIP/GZIP и входные файлы ограничены 50 MiB; CSV — 50 000 строк; PDF — максимум 500 страниц. ZIP не распаковывается на диск, пути из архива используются только как имена metadata.
- Extract from File CSV/JSON/PDF сохраняет результат в `destinationKey`. PDF возвращает извлечённый текст; OCR для сканов не выполняется.
- HTTP поддерживает Response Format `json`, `text`, `file` и `autodetect`, а также `fullResponse` и `neverError`; формы результатов описаны выше. Ответ ограничен 50 MiB. Не поддерживаются authentication/credentials, multipart и binary upload, form-urlencoded body, pagination, proxy и некоторые другие настройки. Для тела запроса поддерживается `jsonBody`; response headers/status доступны через `fullResponse`.
- Data Table поддерживает только `resource=row` и операции `get`, `insert`, `update`, `upsert`, `deleteRows`, `rowExists`, `rowNotExists` с простыми фильтрами. Операции над самой таблицей (создать, перечислить, удалить), сортировка, полноценная схема/конвертация колонок и конкурентно-атомарный upsert не реализованы. `schema`, `matchingColumns` и `optimizeBulk` сейчас не применяются SQL-адаптером, поэтому на них нельзя полагаться. `attemptToConvertTypes=true` и `convertFieldsToString=true` не поддерживаются. `dataTableId` служит ключом в локальном хранилище; перенос между n8n internal tables и созданной PostgreSQL базой нужно отдельно настроить.
- Postgres поддерживает только `executeQuery`; любые другие операции узла, n8n credentials и batch modes не портируются. Запрос должен использовать `$1`, `$2`, ... с `queryReplacement`, значения привязываются параметрами. Подключение передаётся через runtime service, рабочий адаптер рассчитан на `DATABASE_URL` + SQLAlchemy async/`asyncpg`.
- Email поддерживает отправку текста, HTML и binary-вложений, перечисленных в `options.fileAttachments`, включая ZIP-файлы из предыдущего узла. Шаблоны, дополнительные заголовки и импорт n8n SMTP credentials не реализованы; SMTP-конфигурация задаётся через переменные окружения. Ошибка SMTP прерывает выполнение.
- Оба runtime исполняют общий `main` control-flow по FIFO-плану: каждое соединение создаёт отдельный вызов, fan-out изолирует items, а Merge append ждёт все входы в порядке их индексов. По умолчанию запускается каждый trigger один раз; `initial_items` выбирает один или несколько triggers и передаёт начальные items. Python Core не исполняет AI Agent; LangGraph backend исполняет AI Agent v2.2 только с указанными в таблице AI-портами и версиями.
- AI Agent сейчас поддерживает только OpenAI-совместимый DeepSeek Chat Completions endpoint, Calculator Tool с числовыми арифметическими выражениями и Simple Memory только в памяти текущего процесса. Нет streaming, vision/file inputs, произвольных LangChain tools, подключаемых баз памяти, embeddings/vector stores, других model providers и автоматического импорта n8n credentials. Значение API key не копируется в generated project.
- Неподдержанный узел приводит к предупреждению на `validate` и к ошибке при сборке исполняемого реестра. Генератор также записывает такие случаи в `MANUAL_CHANGES.md` и блокирует запуск, пока человек не внесёт и не отметит исправление.
- Ошибки узлов не превращаются в пустой результат. Runtime добавляет имя узла и сохраняет исходное исключение как причину.

## Ручная доработка неподдержанных возможностей

Конвертация неподдержанной версии или конфигурации записывает проблему в `MANUAL_CHANGES.md` с именем узла, его типом и `typeVersion`, причиной, файлом-точкой расширения, местом для настройки credentials и командой запуска. Сгенерированный `main.py` останавливается, пока действие не удалено или не закрыто в `src/generated_workflow/manual_changes.py`; значение секрета при этом не копируется в исходники.

Пользователь может вручную добавить недостающую логику в указанный файл и функцию. Для LangGraph AI модели задаются через `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` и `DEEPSEEK_MODEL`; прочие credentials следует добавлять в `src/generated_workflow/services.py` или собственный адаптер, не записывая секреты в `workflow.json`.

## Проверки

`tests/test_node_converters.py`, `tests/test_extended_node_converters.py` и `tests/test_http_response_shapes.py` используют локальные n8n JSON fixtures. HTTP проверяется локальным `aiohttp` сервером, SMTP — локальным SMTP listener, Data Table/Postgres — настоящим SQLAlchemy async engine на SQLite для детерминированных тестов. Дополнительно `tests/test_postgres_integration.py` запускает проверки с настоящим PostgreSQL при заданной переменной `N8N_TEST_POSTGRES_URL`; без неё эта группа пропускается.

## Как добавить поддержку узла

1. Найдите реальный экспорт с нужным `node.type` и `typeVersion`. Удалите из fixture credentials, персональные данные и приватное содержимое.
2. Опишите проверяемую Pydantic-модель параметров для поддерживаемого подмножества. Неподдерживаемые значения должны выдавать понятную диагностику или попадать в `MANUAL_CHANGES.md`.
3. Реализуйте async executor, который получает `WorkflowNode`, входные `WorkflowItem` и `NodeExecutionContext`, а возвращает `NodeResult`. Для внешних систем используйте runtime services/ports, не записывайте секреты в workflow и не подавляйте ошибки.
4. Зарегистрируйте точную версию в `default_node_converter_registry()`. Не принимайте неизвестные версии широким сравнением вида `typeVersion >= ...`.
5. Добавьте тест модели параметров, тест выхода узла и тест связанного графа. Для HTTP используйте локальный сервер; для DB — локальный SQLAlchemy async engine, а внешнее PostgreSQL подключайте только отдельным явно настраиваемым integration test.
6. Обновите таблицу версий, описание поддержанного подмножества и ограничения этого README. Проверьте обе генерации там, где узел должен работать и в Python Core, и в LangGraph.

Реестр находится в `nodes.py`; общие runtime-контракты — в [runtime README](../runtime/README.md). Поддержка означает проверенную комбинацию версии, параметров и поведения, а не только наличие класса или строки в реестре.
