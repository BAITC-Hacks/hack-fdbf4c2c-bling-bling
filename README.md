# HackAlem AI — протоколирование совещаний

Прототип команды Bling Bling для протоколирования совещаний и выделения поручений.
Главный источник — [ai-review/requirements.md](ai-review/requirements.md).
План — [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## Состояние: локальный Speech-to-Text (этап 4)

Работают FastAPI, конфигурация ENV, создание/чтение метаданных совещаний в SQLite,
статусы подготовки/STT и `/health`. Записи сохраняются после перезапуска, даты создания
возвращаются в UTC. Работают загрузка, подготовка аудио и локальный faster-whisper.
Диаризация, поручения, summary, экспорт и frontend еще не реализованы.
Внешние AI API не вызываются. `/health` проверяет API, а не наличие весов моделей.

## Установка и запуск

Требуется Python 3.11+. Из корня проекта в Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Linux/macOS:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Без тестовых зависимостей достаточно `requirements.txt`. Для загрузки нужен
ffmpeg в PATH или `HACKALEM_FFMPEG_PATH` с путем к исполняемому файлу.
Ollama пока не нужен. Для STT нужны отдельные зависимости и предварительно скачанные
веса (инструкция ниже). Сеть нужна при подготовке, но не при обработке записи.
Для разработки `requirements-dev.txt` включает сборку ffmpeg через imageio-ffmpeg:

```powershell
$env:HACKALEM_FFMPEG_PATH = (& .venv/Scripts/python.exe -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Это настройка пути к локальному бинарнику, а не облачный сервис или mock.

## Конфигурация

`app/core/config.py` использует Pydantic Settings: параметры конструктора → ENV
→ `.env` → значения по умолчанию. `.env` необязателен и исключен из Git.
ENV-файлы с примерами не хранятся в репозитории по указанию пользователя.
Относительные пути разрешаются от корня проекта.

| ENV | По умолчанию | Назначение |
| --- | --- | --- |
| `HACKALEM_APP_NAME` | `HackAlem AI` | Название API |
| `HACKALEM_LOG_LEVEL` | `INFO` | Уровень логов приложения |
| `HACKALEM_DATA_DIR` | `data` | uploads, processed, exports |
| `HACKALEM_DATABASE_PATH` | `<DATA_DIR>/meetings.sqlite3` | SQLite |
| `HACKALEM_FFMPEG_PATH` | `ffmpeg` | Команда из PATH или полный путь |
| `HACKALEM_MAX_UPLOAD_BYTES` | `524288000` | До 500 MiB на файл |
| `HACKALEM_MAX_AUDIO_SECONDS` | `14400` | До 4 часов аудио |
| `HACKALEM_FFMPEG_TIMEOUT_SECONDS` | `600` | Максимальное время преобразования |
| `HACKALEM_DIARIZATION_MODEL_PATH` | `models/speaker-diarization-community-1` | Будущие локальные веса |
| `HACKALEM_OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Только HTTP loopback; пока не вызывается |
| `HACKALEM_OLLAMA_MODEL` | пусто | Задается перед реализацией анализа |

База и каталоги создаются при старте. `.env`, данные, модели и окружение исключены
из Git. Содержимое встреч не логируется приложением. Авторизации пока нет:
предусмотрен локальный запуск на `127.0.0.1`.

## Проверка

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
$meeting = Invoke-RestMethod http://127.0.0.1:8000/meetings -Method Post -ContentType 'application/json' -Body '{"title":"Demo meeting"}'
Invoke-RestMethod "http://127.0.0.1:8000/meetings/$($meeting.id)"
Invoke-RestMethod "http://127.0.0.1:8000/meetings/$($meeting.id)/status"
```

| Endpoint | Результат |
| --- | --- |
| `GET /health` | 200, `{"status":"ok"}` |
| `POST /meetings` | JSON `{"title":"..."}` → 201, id/title/status/created_at |
| `GET /meetings/{id}` | 200, сохраненная запись |
| `GET /meetings/{id}/status` | 200, id/status |
| `POST /meetings/{id}/upload` | multipart поле `file` → 200, параметры подготовленного WAV |
| `POST /meetings/{id}/transcribe` | Без тела → синхронный локальный STT, 200 с транскриптом |
| `GET /meetings/{id}/transcript` | 200 с сохраненным транскриптом |

Название после удаления крайних пробелов: 1–200 символов. Неизвестные поля
и неправильный UUID дают 422; отсутствующий UUID — 404. Создание записи не
запускает обработку. OpenAPI доступен по `/openapi.json`; Swagger/ReDoc отключены,
чтобы не обращаться к CDN.

## Загрузка аудио/видео

После создания совещания:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/meetings/$($meeting.id)/upload" -F "file=@C:/recordings/meeting.mp4"
```

Поддержаны wav, mp3, m4a, mp4, webm (регистр расширения не важен).
ffmpeg читает первую аудиодорожку и создает WAV PCM signed 16-bit, mono, 16000 Hz.
Паузы не вырезаются; тишина с ненулевой длительностью допустима. Нулевая длина
файла или отсутствие аудиосэмплов считается пустой записью. Видео без аудиодорожки
отклоняется. Сохраняются исходный файл в `data/uploads/` и WAV в `data/processed/`;
имена генерирует сервер. Пользовательское имя не используется как путь.
Пути и длительность сохраняются в таблице `meeting_audio`. Она создается и в
существующей SQLite без изменения таблицы совещаний.

Пример ответа: `{"id":"UUID","status":"audio_ready","format":"wav","channels":1,"sample_rate":16000,"duration_seconds":12.5}`.
Запрос синхронный: дождитесь завершения ffmpeg. Во время обработки статус
`uploading`; при успехе `audio_ready`, при ошибке снова `created` с удалением
частичных файлов. Повторная/одновременная загрузка для занятого или готового
совещания дает 409; для новой записи создайте другое совещание.

Ошибки имеют `detail.code` и понятное `detail.message`:

| HTTP | Код | Причина |
| --- | --- | --- |
| 415 | unsupported_format | Расширение не поддерживается |
| 422 | invalid_media | Поврежденный файл, неверный контейнер или нет аудио |
| 422 | empty_recording | Пустой файл или ноль аудиосэмплов |
| 503 | ffmpeg_not_found / ffmpeg_unavailable | ffmpeg отсутствует или не запускается |
| 413 | file_too_large / recording_too_long | Превышен размер или длительность |
| 504 | conversion_timeout | Истек таймаут |
| 507 | storage_error | Не удалось сохранить файл |
| 409 | upload_conflict | Совещание уже обрабатывается или аудио подготовлено |

ffmpeg запускается без shell, с явным демультиплексором и разрешением только
протокола `file`; сетевые источники и плейлисты не принимаются.
Лимиты являются настройками реализации, а не требованиями организаторов.
Multipart разбирается библиотекой до вызова обработчика (большие файлы временно
буферизуются на диске); для публичного размещения дополнительно нужен лимит
тела запроса на входном сервере. MVP рассчитан на локальный доверенный доступ.
При аварийном завершении процесса посреди загрузки возможны временные файлы
и статус `uploading`; автоматическое восстановление еще не реализовано.
Перед записью участники должны быть уведомлены о записи и ИИ-транскрибации;
реальные демонстрационные записи должны быть анонимизированы.

## Структура и тесты

`app/main.py` — lifecycle; `app/api/` — HTTP; `app/core/` — ENV и логи;
`app/models/` — SQLAlchemy; `app/schemas/` — Pydantic; `app/repositories/` —
данные; `app/db/` — сессии; `app/services/` — подготовка аудио и STT.
Зарезервированы `frontend/`, `scripts/`, `tests/fixtures/`.
Локальные данные находятся в `data/uploads/`, `data/processed/`, `data/exports/`.

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe scripts/smoke.py
```

Linux/macOS: `.venv/bin/python -m pytest -q`. Тесты используют временную базу
и проверяют API, перезапуск, валидацию, ENV и запрет внешнего адреса Ollama.
Автотесты используют синтетические записи и явно выделенные doubles STT. Результаты
отдельного реального прогона на открытых данных описаны в `docs/STT_EVALUATION.md`.
Развернутой версии нет; полный сценарий до поручений и экспорта пока не реализован.

## Speech-to-Text: подготовка модели

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-stt.txt
$env:WHISPER_MODEL = 'large-v3'
$env:WHISPER_DEVICE = 'auto'
$env:WHISPER_COMPUTE_TYPE = 'auto'
.venv/Scripts/python.exe scripts/prepare_whisper.py
```

Скачивание выполняется **до** обработки встреч. Весам нужен диск и достаточно
памяти; на слабом CPU можно выбрать `small`/`medium` либо совместимую локальную
модель, включая `large-v3-turbo`. Точность при смене модели нужно проверять.
Для этого компьютера скачана и проверена `small`; `large-v3` не скачана.

```powershell
$env:WHISPER_MODEL = 'small'
$env:WHISPER_DEVICE = 'cpu'
$env:WHISPER_COMPUTE_TYPE = 'int8'
.venv/Scripts/python.exe scripts/prepare_whisper.py
# Задайте ffmpeg как в разделе установки, если его нет в PATH.
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Модель по умолчанию хранится в `models/whisper/<model>/`; для repo ID служебные
символы заменяются на `_`. `WHISPER_MODEL_PATH` задает другой локальный каталог.
Он должен содержать CTranslate2-веса, config.json, tokenizer.json и vocabulary.txt
или vocabulary.json. При наличии используется preprocessor_config.json; для
моделей, которые его не поставляют, действуют defaults faster-whisper.
Нет скачивания при inference: используется локальный путь и `local_files_only=True`.
В отсутствие tokenizer.json адаптер завершится ошибкой до загрузки библиотеки,
не позволяя ее стандартному fallback скачать токенизатор.

## Централизованные настройки STT

Все настройки находятся в `app/core/config.py`. Измените ENV и перезапустите сервер:
настройки и модель фиксируются на срок жизни процесса. `.env` не обязателен.

| ENV | По умолчанию | Назначение |
| --- | --- | --- |
| `WHISPER_MODEL` | `large-v3` | Имя модели/ID для подготовки и метаданных |
| `WHISPER_MODEL_PATH` | `models/whisper/<model>` | Локальный каталог CTranslate2 |
| `WHISPER_DEVICE` | `auto` | `cpu`/`cuda`/автовыбор по наличию CUDA |
| `WHISPER_COMPUTE_TYPE` | `auto` | `auto`: CPU int8, CUDA float16; можно задать поддерживаемый тип явно |
| `WHISPER_MULTILINGUAL` | `true` | Повторное определение языка в окнах декодирования |
| `WHISPER_LANGUAGE` | `auto` | Для смешанной речи остается auto; ru/kk допустимы только при multilingual=false |
| `WHISPER_WORD_TIMESTAMPS` | `true` | Временные метки слов, если доступны |
| `WHISPER_VAD_FILTER` | `true` | Локальный Silero VAD, поставляемый с faster-whisper |
| `WHISPER_BEAM_SIZE` | `5` | Ширина beam search |
| `WHISPER_CHUNK_LENGTH` | `30` | Длина окна в секундах, 1–30; настройка важна для code-switching |
| `WHISPER_CPU_THREADS` | `4` | Потоки CPU |
| `WHISPER_CONDITION_ON_PREVIOUS_TEXT` | `false` | Не закреплять язык предыдущих окон через текстовый контекст |

Старые `HACKALEM_STT_MODEL`, `HACKALEM_STT_DEVICE`, `HACKALEM_STT_COMPUTE_TYPE`
поддерживаются как aliases; новые `WHISPER_*` при одновременном задании имеют приоритет.
Режим перевода не настраивается: всегда `task="transcribe"`. Постобработки с переводом нет.
English-only модели отклоняются. При явном CUDA без CUDA возвращается ошибка,
скрытого переключения на CPU или облако нет. Для GPU необходимы совместимые
NVIDIA-библиотеки CTranslate2 (см. [инструкцию faster-whisper](https://github.com/SYSTRAN/faster-whisper#gpu)).

Используется обычный `WhisperModel.transcribe`, а не batched pipeline.
`multilingual=True` переопределяет язык по окнам, не по каждому слову;
одна выходная реплика не обязана совпадать с окном. Метаданные `detected_language`
и `language_probability` относятся к первичному определению, а не ко всем словам
смешанной записи. Достоверные языковые метки отдельных сегментов библиотека
не предоставляет — они не выдумываются. Временные метки VAD восстанавливаются
относительно исходного подготовленного WAV. [Код faster-whisper](https://github.com/SYSTRAN/faster-whisper/blob/v1.2.1/faster_whisper/transcribe.py).

## Запуск STT и результат

После создания встречи и успешной загрузки:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/meetings/$($meeting.id)/transcribe" -Method Post
Invoke-RestMethod "http://127.0.0.1:8000/meetings/$($meeting.id)/transcript"
```

Результат сохраняется атомарно в **`data/processed/{meeting_id}/transcript_raw.json`**
(либо под настроенным DATA_DIR). JSON в UTF-8 содержит полный `text`, `segments`
с `text/start/end`, необязательные `words` с `text/start/end/probability`, язык,
вероятность языка, длительность, модель, устройство, compute type и параметры.
Пустой результат без распознанной речи сохраняется с предупреждением, без выдуманного текста.

Статусы: `audio_ready → transcribing → transcribed`. При штатной ошибке статус
снова `audio_ready`, исходный WAV сохраняется, повтор возможен. Одновременно
исполняется один STT-запрос на процесс; занятость и повтор уже завершенной встречи
возвращают 409. Завершенный результат читается через GET. Для повторного эксперимента
с другими настройками создайте новую встречу.

Отсутствующие/неполные веса и зависимости дают 503 с `detail.code` (`model_not_ready`,
`stt_dependency_missing`); проблемы CUDA/compute — `cuda_unavailable` или
`unsupported_compute_type`; сбой inference — 500 `transcription_failed`;
ошибка сохранения — 507. Текст записи и внутренние ошибки модели не отправляются в логи API.
Распознавание синхронное, без очереди и восстановления после аварийного завершения;
зависший после аварии `transcribing` автоматически не сбрасывается. Эти ограничения
не позволяют считать каркас промышленным сервисом.

Локальная оценка на собственных разрешенных примерах:

```powershell
.venv/Scripts/python.exe scripts/evaluate_stt.py data/stt-evaluation/manifest.json
```

Manifest — список объектов `name`, `audio` (путь относительно manifest), `reference`.
Скрипт не загружает данные и блокирует Python socket.connect во время оценки;
это не заменяет системный firewall. Отчет с текстами и WER остается под `data/` вне Git.
