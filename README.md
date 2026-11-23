# Spa WhatsApp

Automatización de WhatsApp para un spa: multi-agente (humanos + IA), citas y recordatorios. Twilio + PostgreSQL + z.ai (glm).

El agente de IA clasifica la intención de cada mensaje entrante (reservar / reprogramar / cancelar / FAQ / derivar), responde automáticamente cuando confía, y deriva a un operador humano cuando no. Los operadores gestionan la bandeja, el calendario y las citas desde una web server-rendered (HTMX + Jinja).

## Arquitectura

```
Twilio WhatsApp ──webhook firmado──▶ ConversationService
                                        │
                          ┌─────────────┴──────────────┐
                          ▼                            ▼
                   AIAgentService (z.ai)        repos (PostgreSQL async)
                   classify_intent + reply          │
                          │                          │
                   decide_routing (pure)             │
                          │                          │
        ┌─────────────────┴─────────────────┐        │
        ▼ (IA)                          ▼ (humano)   │
   generate_reply + envía WA      AWAITING_HUMAN      │
                                   (bandeja web)      │
                                                     │
   AppointmentService ◀── CalendarService (slots) ◀── web UI
        │
   ReminderService ──add_job──▶ AsyncScheduler (APScheduler + SQLAlchemyJobStore)
                                       │ fire
                                       ▼
                          send_reminder(reminder_id) ─▶ WhatsApp / EmailService(SMTP)
```

Capas (DRY, cada frontera es inyectable y testeable):

- **`app/db/`** — modelos SQLAlchemy 2.0 async, `Base`, `get_session`, `BaseRepository[T]` + subclases finas.
- **`app/services/`** — `whatsapp_service` (Twilio, single source of truth), `appointment_service`, `reminder_service` (specs puras + scheduler port), `ai_agent_service` (z.ai, frontera `ZaiClient`), `conversation_service` (routing), `calendar_service` (motor de slots), `email_service` (SMTP).
- **`app/auth/`** — JWT (PyJWT) + RBAC + bcrypt.
- **`app/api/`** — `webhooks` (Twilio, firmado), `auth` (login/me).
- **`app/web/`** — UI server-rendered (Jinja2 + HTMX), sesión por cookie firmada (itsdangerous).
- **`app/scheduler/`** — adapter APScheduler (`SchedulerPort`).
- **`app/jobs/`** — `send_reminder` (target del scheduler).
- **`app/main.py`** — ensambla routers + lifespan (arranca scheduler, recupera reminders pendientes).

## Stack

Python 3.11+ · FastAPI · SQLAlchemy 2.0 async (asyncpg) · PostgreSQL · Alembic · Twilio · APScheduler (SQLAlchemyJobStore) · PyJWT · bcrypt · Jinja2 + HTMX · aiosmtplib · z.ai (glm).

## Puesta en marcha rápida (Docker)

```bash
cp .env.example .env          # rellena TWILIO_*, ZAI_API_KEY, JWT_SECRET, SMTP_*
docker compose up --build      # postgres + app en :8000
```

El contenedor `app` ejecuta `scripts/init_db.py` (crea tablas) y luego `uvicorn app.main:app`. El lifespan arranca APScheduler y recupera los reminders pendientes.

## Puesta en marcha local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# PostgreSQL corriendo en localhost:5432 (ver docker compose up postgres)
python scripts/init_db.py     # crea las tablas (dev)

uvicorn app.main:app --reload --port 8000
```

## Variables de entorno

Ver `.env.example`. Las esenciales:

| Variable | Para qué |
| --- | --- |
| `DATABASE_URL` | Postgres async (`postgresql+asyncpg://...`). En compose se sobreescribe al host `postgres`. |
| `JWT_SECRET` | Firma de tokens JWT y cookies web. **Cámbialo en prod** (`openssl rand -hex 32`). |
| `BUSINESS_TZ` | Zona horaria del spa para ventanas de Availability y slots (p. ej. `Europe/Madrid`). |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_WHATSAPP_FROM` | Envío + verificación de firma del webhook. Sin `AUTH_TOKEN` el webhook devuelve 403 (fail-closed). |
| `ZAI_API_KEY` / `ZAI_BASE_URL` / `ZAI_MODEL` | Agente IA. Sin key, el servicio usa el CLI `z-ai` si está disponible (dev). `AI_AGENT_ENABLED=false` deriva todo a humano. |
| `SMTP_*` | Email de respaldo de recordatorios (`EMAIL_BACKUP`). |

## Webhook de Twilio

Configura en Twilio tu número de WhatsApp sandbox/dedicado con webhook:

```
POST  {APP_BASE_URL}/webhooks/whatsapp
```

El endpoint verifica `X-Twilio-Signature` (HMAC del AuthToken) sobre la URL + params. Tras un proxy TLS-terminador, asegúrate de que `X-Forwarded-Proto` se respeta para que la URL firmada coincida.

## Pruebas

```bash
pytest                          # suite completa (Tier A puros + glue + fakes)
```

- **Tier A**: lógica pura (slots, parsing JSON de IA, routing, JWT, bcrypt, reminder specs) + glue con fakes en las fronteras (Twilio, z.ai, SMTP, scheduler).
- **Tier B**: PostgreSQL real (citas, repos, conversation, calendario, jobs, login web). Se auto-saltan sin DB (`pytest` las salta; levanta Postgres para ejecutarlas).

Verificación real del agente IA contra z.ai en vivo:

```bash
PYTHONPATH=. python scripts/verify_ai_integration.py
```

## Estructura

```
app/
  api/          webhooks.py, auth.py
  auth/         security.py, jwt_handler.py, dependencies.py
  db/           base.py, models.py, repositories.py
  jobs/         reminder_jobs.py
  scheduler/    adapter.py
  services/     whatsapp, appointment, reminder, ai_agent, conversation, calendar, email
  web/          routes.py, session.py, templates/
  alembic/      env.py (async, target_metadata=Base.metadata)
  config.py, main.py
scripts/        init_db.py, verify_ai_integration.py
tests/          por módulo
```

## Roadmap

- `GOOGLE_SYNC_ENABLED=false` (MVP): el motor de slots es 100% local; la interfaz de `CalendarService` admite un adapter de Google Calendar sin tocar `compute_free_slots`.
- Recover de reminders past-due al arrancar (disparo inmediato o marca `skipped`).
