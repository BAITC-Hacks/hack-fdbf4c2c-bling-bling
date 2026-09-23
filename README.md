# HackAlem AI — протоколирование совещаний

Прототип команды Bling Bling для протоколирования совещаний и выделения поручений.
Главный источник — [ai-review/requirements.md](ai-review/requirements.md).
План — [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## Состояние: этап 1

Работают FastAPI, конфигурация ENV, создание/чтение метаданных совещаний в SQLite,
статус `created` и `/health`. Записи сохраняются после перезапуска, даты создания
возвращаются в UTC. Обработка аудио, STT, диаризация, поручения, summary, экспорт
и frontend еще не реализованы. Модели и внешние AI API не вызываются.
`/health` проверяет работу API, а не готовность будущих моделей.

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

Без тестовых зависимостей достаточно `requirements.txt`. ffmpeg, Ollama и веса
для этапа 1 не нужны. Сеть нужна при установке, но не при работе API.

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
| `HACKALEM_STT_MODEL` | `large-v3` | Будущий STT |
| `HACKALEM_STT_DEVICE` | `cpu` | cpu/cuda/auto |
| `HACKALEM_STT_COMPUTE_TYPE` | `int8` | Будущий STT |
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

Название после удаления крайних пробелов: 1–200 символов. Неизвестные поля
и неправильный UUID дают 422; отсутствующий UUID — 404. Создание записи не
запускает обработку. OpenAPI доступен по `/openapi.json`; Swagger/ReDoc отключены,
чтобы не обращаться к CDN.

## Структура и тесты

`app/main.py` — lifecycle; `app/api/` — HTTP; `app/core/` — ENV и логи;
`app/models/` — SQLAlchemy; `app/schemas/` — Pydantic; `app/repositories/` —
данные; `app/db/` — сессии; `app/services/` — будущий pipeline.
Зарезервированы `frontend/`, `scripts/`, `tests/fixtures/`.
Локальные данные находятся в `data/uploads/`, `data/processed/`, `data/exports/`.

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe scripts/smoke.py
```

Linux/macOS: `.venv/bin/python -m pytest -q`. Тесты используют временную базу
и проверяют API, перезапуск, валидацию, ENV и запрет внешнего адреса Ollama.
Реальные записи не используются. Развернутой версии нет; end-to-end AI-сценарий
пока не реализован.
