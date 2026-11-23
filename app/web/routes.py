"""Rutas web (HTMX + Jinja2). UI para operadores: inbox, calendario, citas, agentes.

Server-rendered; HTMX carga parciales (_messages, _slots, filas) sin recargar.
Auth por cookie firmada (app.web.session). Responsive (Tailwind CDN).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import authenticate
from app.config import settings
from app.db.base import get_session
from app.db.models import User, UserRole
from app.db.repositories import (
    AgentRepository,
    AppointmentRepository,
    ClientRepository,
    ConversationRepository,
    MessageRepository,
    ServiceRepository,
    StaffRepository,
)
from app.services.appointment_service import AppointmentConflictError, AppointmentService
from app.services.calendar_service import CalendarService
from app.services.conversation_service import ConversationService
from app.services.reminder_service import NullScheduler, ReminderService
from app.services.whatsapp_service import WhatsAppService, get_whatsapp_service
from app.services.ai_agent_service import get_ai_agent_service
from app.web.session import (
    COOKIE_NAME,
    clear_session_cookie,
    current_user,
    require_web_admin,
    set_session_cookie,
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(tags=["web"])


def _ctx(request: Request, user: User | None = None, **extra) -> dict:
    return {"request": request, "user": user, "business_tz": settings.business_tz, **extra}


def _conversation_service(session: AsyncSession) -> ConversationService:
    return ConversationService(session, get_ai_agent_service(), get_whatsapp_service())


def _appointment_service(session: AsyncSession) -> AppointmentService:
    return AppointmentService(session, ReminderService(session, NullScheduler()))


# --- Auth ---

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", _ctx(request))


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    user = await authenticate(email, password, _user_repo(session))
    resp = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(resp, user.id)
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(resp)
    return resp


def _user_repo(session: AsyncSession):
    from app.db.repositories import UserRepository
    return UserRepository(session)


# --- Dashboard ---

@router.get("/", response_class=HTMLResponse)
async def index(user: User = Depends(current_user)):
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    convs = await ConversationRepository(session).list_awaiting_human()
    now = datetime.now(timezone.utc)
    upcoming = await AppointmentRepository(session).list_upcoming(now=now, limit=5)
    return templates.TemplateResponse(
        "dashboard.html", _ctx(request, user, awaiting=len(convs), upcoming=upcoming, now=now)
    )


# --- Inbox ---

@router.get("/inbox", response_class=HTMLResponse)
async def inbox(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    convs = await ConversationRepository(session).list_awaiting_human()
    return templates.TemplateResponse("inbox.html", _ctx(request, user, conversations=convs))


@router.get("/inbox/{conv_id}", response_class=HTMLResponse)
async def conversation_detail(
    request: Request,
    conv_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    conv = await ConversationRepository(session).get(conv_id)
    if conv is None:
        raise HTTPException(404, "Conversacion no encontrada")
    msgs = await MessageRepository(session).list_by_conversation(conv_id)
    client = await ClientRepository(session).get(conv.client_id)
    return templates.TemplateResponse(
        "_conversation_detail.html",
        _ctx(request, user, conv=conv, messages=msgs, client=client),
    )


@router.post("/inbox/{conv_id}/reply", response_class=HTMLResponse)
async def reply_conversation(
    request: Request,
    conv_id: int,
    body: str = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    whatsapp: WhatsAppService = Depends(get_whatsapp_service),
):
    conv_svc = ConversationService(
        session, get_ai_agent_service(), whatsapp,
        clients=ClientRepository(session),
        conversations=ConversationRepository(session),
        messages=MessageRepository(session),
        agents=AgentRepository(session),
    )
    await conv_svc.send_human_reply(conv_id, body, sender_agent_id=user.agent_id)
    await session.commit()
    msgs = await MessageRepository(session).list_by_conversation(conv_id)
    conv = await ConversationRepository(session).get(conv_id)
    return templates.TemplateResponse(
        "_messages.html", _ctx(request, user, conv=conv, messages=msgs)
    )


# --- Calendario (slots + reserva) ---

@router.get("/calendar", response_class=HTMLResponse)
async def calendar(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    staff_list = await StaffRepository(session).list_active()
    services = await ServiceRepository(session).list_active()
    return templates.TemplateResponse(
        "calendar.html", _ctx(request, user, staff_list=staff_list, services=services)
    )


@router.get("/calendar/slots", response_class=HTMLResponse)
async def calendar_slots(
    request: Request,
    staff_id: int = Query(...),
    service_id: int = Query(...),
    day: str = Query(...),
    client_phone: str = Query(...),
    client_name: str = Query(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    # HTMX hx-get envia los campos del form como query string.
    cal = CalendarService(session)
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return templates.TemplateResponse("_slots.html", _ctx(request, user, slots=[], error="Fecha invalida"))
    slots = await cal.list_available_slots(staff_id, service_id, d)
    return templates.TemplateResponse(
        "_slots.html",
        _ctx(request, user, slots=slots, staff_id=staff_id, service_id=service_id,
             client_phone=client_phone, client_name=client_name, day=day),
    )


@router.post("/calendar/book", response_class=HTMLResponse)
async def calendar_book(
    request: Request,
    staff_id: int = Form(...),
    service_id: int = Form(...),
    starts_at: str = Form(...),
    client_phone: str = Form(...),
    client_name: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        start = datetime.fromisoformat(starts_at)
    except ValueError:
        return templates.TemplateResponse("_book_result.html", _ctx(request, user, error="Fecha/hora invalida"))
    client = await ClientRepository(session).get_or_create_by_phone(
        client_phone, name=client_name or None
    )
    appt_svc = _appointment_service(session)
    try:
        appt = await appt_svc.create_appointment(
            client_id=client.id, service_id=service_id, staff_id=staff_id,
            starts_at=start, created_by_agent_id=user.agent_id,
        )
    except AppointmentConflictError:
        return templates.TemplateResponse(
            "_book_result.html", _ctx(request, user, error="Ese slot ya no esta disponible (conflicto)")
        )
    except ValueError as exc:
        return templates.TemplateResponse("_book_result.html", _ctx(request, user, error=str(exc)))
    await session.commit()
    return templates.TemplateResponse(
        "_book_result.html", _ctx(request, user, appt=appt, success=True)
    )


# --- Citas ---

@router.get("/appointments", response_class=HTMLResponse)
async def appointments(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    now = datetime.now(timezone.utc)
    appts = await AppointmentRepository(session).list_upcoming(now=now, limit=50)
    return templates.TemplateResponse("appointments.html", _ctx(request, user, appointments=appts))


@router.post("/appointments/{appt_id}/cancel", response_class=HTMLResponse)
async def cancel_appointment(
    request: Request,
    appt_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    appt_svc = _appointment_service(session)
    try:
        await appt_svc.cancel_appointment(appt_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await session.commit()
    return templates.TemplateResponse("_appointment_cancelled.html", _ctx(request, user, appt_id=appt_id))


# --- Agentes (admin) ---

@router.get("/agents", response_class=HTMLResponse)
async def agents(
    request: Request,
    user: User = Depends(require_web_admin),
    session: AsyncSession = Depends(get_session),
):
    all_agents = await AgentRepository(session).get_multi(order_by=AgentRepository.model.id)
    ai_agent = await AgentRepository(session).get_ai_agent()
    return templates.TemplateResponse(
        "agents.html", _ctx(request, user, agents=all_agents, ai_agent=ai_agent)
    )
