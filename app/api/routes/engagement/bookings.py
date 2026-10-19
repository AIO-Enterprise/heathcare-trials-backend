"""
Appointment Booking Routes

GET  /api/advertisements/{ad_id}/appointments/slots?date=YYYY-MM-DD
  — Returns available time slots for a date (public, no auth).

  Slot availability rules (all three must pass):
    1. Date must be within the campaign's trial_start_date … trial_end_date window.
       If dates are not set, defaults to today … today+30 days.
    2. Slot time must be between 09:00 and 17:00 and not in the past.
    3. Number of confirmed bookings for that slot must be < max_per_slot.
       max_per_slot comes from booking_config.max_per_slot (default 3).

POST /api/advertisements/{ad_id}/appointments
  — Books a slot (public, no auth). Returns 409 if slot is at capacity.

GET  /api/advertisements/{ad_id}/appointments
  — Lists all appointments for a campaign (study coordinator / PM auth required).

PATCH /api/advertisements/{ad_id}/appointments/{appointment_id}
  — Cancel or update an appointment (study coordinator auth required).

GET  /api/advertisements/{ad_id}/booking-config
  — Return current booking_config merged with campaign dates (study coordinator).

PATCH /api/advertisements/{ad_id}/booking-config
  — Update slot_duration_minutes and/or max_per_slot (study coordinator).
"""

import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_company_ad_or_404, get_current_user, require_roles
from app.core.timeutils import ad_zone, naive_now, to_naive_local, today_in
from app.db.database import get_db
from app.models.models import Advertisement, Appointment, User, UserRole
from app.schemas.schemas import AppointmentCreate, AppointmentOut, AvailableSlotsResponse, SlotInfo
from app.services.messaging import sms_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["appointments"])

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_SLOT_DURATION = 30  # minutes
DEFAULT_MAX_PER_SLOT = 3  # confirmed bookings before a slot becomes full
DAY_START_HOUR = 9  # 09:00
DAY_END_HOUR = 17  # 17:00 (last slot must finish by this time)
DEFAULT_BOOKING_DAYS = 30  # days ahead when no end date is set


# ── Helpers ───────────────────────────────────────────────────────────────────


def _booking_config(ad: Advertisement) -> dict:
    """Return resolved booking config with defaults filled in."""
    bc = ad.booking_config if isinstance(ad.booking_config, dict) else {}
    return {
        "slot_duration_minutes": int(bc.get("slot_duration_minutes") or DEFAULT_SLOT_DURATION),
        "max_per_slot": int(bc.get("max_per_slot") or DEFAULT_MAX_PER_SLOT),
    }


def _campaign_window(ad: Advertisement) -> tuple[date, date]:
    """
    Return (window_start, window_end) as date objects in campaign-local time.
    Falls back to today … today+30 when either date is missing.
    """
    zone = ad_zone(ad)
    today = today_in(zone)
    start = ad.trial_start_date if ad.trial_start_date else today
    end = ad.trial_end_date if ad.trial_end_date else today + timedelta(days=DEFAULT_BOOKING_DAYS)
    # Clamp start so we never show past-only windows
    if start < today:
        start = today
    return start, end


def _generate_slots(duration: int) -> list[dict]:
    """Generate {time: 'HH:MM', label: 'H:MM AM/PM'} from 09:00 to 17:00."""
    slots = []
    t = DAY_START_HOUR * 60
    end = DAY_END_HOUR * 60
    while t + duration <= end:
        h, m = divmod(t, 60)
        period = "AM" if h < 12 else "PM"
        display_h = h if 1 <= h <= 12 else (12 if h == 0 else h - 12)
        slots.append(
            {
                "time": f"{h:02d}:{m:02d}",
                "label": f"{display_h}:{m:02d} {period}",
            }
        )
        t += duration
    return slots


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/advertisements/{ad_id}/appointments/slots", response_model=AvailableSlotsResponse)
async def get_slots(
    ad_id: str,
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    db: AsyncSession = Depends(get_db),
):
    ad = await db.get(Advertisement, ad_id)
    if not ad:
        raise HTTPException(status_code=404, detail="Campaign not found")

    try:
        req_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    # Rule 1 — date must be within the campaign booking window
    win_start, win_end = _campaign_window(ad)
    if req_date < win_start or req_date > win_end:
        # Return an empty slot list rather than a 400 — the frontend can
        # show "no availability" without crashing.
        bc = _booking_config(ad)
        return JSONResponse(
            content=AvailableSlotsResponse(date=date, duration_minutes=bc["slot_duration_minutes"], slots=[]).model_dump(),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    bc = _booking_config(ad)
    duration = bc["slot_duration_minutes"]
    capacity = bc["max_per_slot"]
    all_slots = _generate_slots(duration)

    # Fetch confirmed booking counts per slot time for the requested date
    day_start = datetime(req_date.year, req_date.month, req_date.day, 0, 0, 0)
    day_end = day_start + timedelta(days=1)

    count_result = await db.execute(
        select(Appointment.slot_datetime, func.count(Appointment.id).label("cnt"))
        .where(
            Appointment.advertisement_id == ad_id,
            Appointment.status == "confirmed",
            Appointment.slot_datetime >= day_start,
            Appointment.slot_datetime < day_end,
        )
        .group_by(Appointment.slot_datetime)
    )
    booking_counts: dict[str, int] = {row.slot_datetime.strftime("%H:%M"): row.cnt for row in count_result.all()}

    now_local = naive_now(ad_zone(ad))

    slot_list = []
    for s in all_slots:
        slot_dt = datetime(req_date.year, req_date.month, req_date.day, *map(int, s["time"].split(":")))
        # Rule 2 — slot must not be in the past (campaign-local time)
        past = slot_dt <= now_local
        # Rule 3 — slot must not be at capacity
        booked = booking_counts.get(s["time"], 0)
        full = booked >= capacity

        slot_list.append(
            SlotInfo(
                time=s["time"],
                label=s["label"],
                available=not past and not full,
            )
        )

    return JSONResponse(
        content=AvailableSlotsResponse(date=date, duration_minutes=duration, slots=slot_list).model_dump(),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.post("/advertisements/{ad_id}/appointments", response_model=AppointmentOut, status_code=201)
async def book_appointment(
    ad_id: str,
    body: AppointmentCreate,
    db: AsyncSession = Depends(get_db),
):
    ad = await db.get(Advertisement, ad_id)
    if not ad:
        raise HTTPException(status_code=404, detail="Campaign not found")

    zone = ad_zone(ad)
    slot_dt = to_naive_local(body.slot_datetime, zone)  # naive campaign-local wall time

    # Rule 1 — date must be within booking window
    win_start, win_end = _campaign_window(ad)
    req_date = slot_dt.date()
    if req_date < win_start or req_date > win_end:
        raise HTTPException(
            status_code=400,
            detail=f"Slot date {req_date} is outside the campaign booking window ({win_start} to {win_end}).",
        )

    # Rule 2 — slot must not be in the past (campaign-local time)
    if slot_dt <= naive_now(zone):
        raise HTTPException(status_code=400, detail="Cannot book a slot in the past.")

    bc = _booking_config(ad)
    duration = bc["slot_duration_minutes"]
    capacity = bc["max_per_slot"]

    # Rule 3 — slot must not be at capacity.
    # Atomicity: lock the campaign row first (SELECT … FOR UPDATE — a no-op on
    # SQLite, which serializes writers anyway) so two concurrent bookings can't
    # both read count < capacity and overshoot max_per_slot.
    lock_result = await db.execute(select(Advertisement).where(Advertisement.id == ad_id).with_for_update())
    lock_result.scalar_one_or_none()

    count_result = await db.execute(
        select(func.count(Appointment.id)).where(
            Appointment.advertisement_id == ad_id,
            Appointment.status == "confirmed",
            Appointment.slot_datetime == slot_dt,
        )
    )
    current_count = count_result.scalar_one()
    if current_count >= capacity:
        raise HTTPException(
            status_code=409,
            detail="This slot is fully booked. Please choose another time.",
        )

    appt = Appointment(
        advertisement_id=ad_id,
        survey_response_id=body.survey_response_id,
        patient_name=body.patient_name,
        patient_phone=body.patient_phone,
        slot_datetime=slot_dt,
        duration_minutes=duration,
        status="confirmed",
        notes=body.notes,
    )
    db.add(appt)
    await db.commit()
    await db.refresh(appt)

    # Fire a confirmation SMS (best-effort — never blocks the booking).
    if appt.patient_phone:
        await sms_service.send_and_log(
            db,
            appt.patient_phone,
            sms_service.confirmation_body(appt.patient_name, appt.slot_datetime, ad.title or ""),
            kind="confirmation",
            appointment_id=appt.id,
            company_id=ad.company_id,
        )
    return appt


@router.get("/advertisements/{ad_id}/appointments", response_model=list[AppointmentOut])
async def list_appointments(
    ad_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in (UserRole.STUDY_COORDINATOR, UserRole.PROJECT_MANAGER):
        raise HTTPException(status_code=403, detail="Access denied")
    # Tenant scope — cross-tenant ids resolve to 404.
    await get_company_ad_or_404(db, current_user, ad_id)

    result = await db.execute(
        select(Appointment).where(Appointment.advertisement_id == ad_id).order_by(Appointment.slot_datetime)
    )
    return result.scalars().all()


class AppointmentPatch(BaseModel):
    status: str | None = Field(None, pattern="^(confirmed|cancelled)$")
    notes: str | None = None


@router.patch("/advertisements/{ad_id}/appointments/{appointment_id}", response_model=AppointmentOut)
async def update_appointment(
    ad_id: str,
    appointment_id: str,
    body: AppointmentPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.PROJECT_MANAGER])),
):
    # Tenant scope — verify the parent campaign belongs to the caller first.
    ad = await get_company_ad_or_404(db, current_user, ad_id)
    result = await db.execute(
        select(Appointment).where(
            Appointment.id == appointment_id,
            Appointment.advertisement_id == ad_id,
        )
    )
    appt = result.scalar_one_or_none()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    was_confirmed = appt.status == "confirmed"
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(appt, field, value)
    await db.commit()
    await db.refresh(appt)

    # Notify the patient when a confirmed appointment is cancelled.
    if was_confirmed and appt.status == "cancelled" and appt.patient_phone:
        await sms_service.send_and_log(
            db,
            appt.patient_phone,
            sms_service.cancellation_body(appt.patient_name, appt.slot_datetime),
            kind="cancellation",
            appointment_id=appt.id,
            company_id=ad.company_id,
        )
    return appt


# ── Booking config management ─────────────────────────────────────────────────


class BookingConfigOut(BaseModel):
    slot_duration_minutes: int
    max_per_slot: int
    window_start: str | None = None  # ISO date or null
    window_end: str | None = None


class BookingConfigPatch(BaseModel):
    slot_duration_minutes: int | None = Field(None, ge=5, le=480)
    max_per_slot: int | None = Field(None, ge=1, le=500)


@router.get("/advertisements/{ad_id}/booking-config", response_model=BookingConfigOut)
async def get_booking_config(
    ad_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR, UserRole.PROJECT_MANAGER])),
):
    ad = await get_company_ad_or_404(db, current_user, ad_id)

    bc = _booking_config(ad)
    win_start, win_end = _campaign_window(ad)
    return BookingConfigOut(
        slot_duration_minutes=bc["slot_duration_minutes"],
        max_per_slot=bc["max_per_slot"],
        window_start=str(win_start),
        window_end=str(win_end),
    )


@router.patch("/advertisements/{ad_id}/booking-config", response_model=BookingConfigOut)
async def update_booking_config(
    ad_id: str,
    body: BookingConfigPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles([UserRole.STUDY_COORDINATOR])),
):
    ad = await get_company_ad_or_404(db, current_user, ad_id)

    existing = ad.booking_config if isinstance(ad.booking_config, dict) else {}
    updated = {**existing}
    if body.slot_duration_minutes is not None:
        updated["slot_duration_minutes"] = body.slot_duration_minutes
    if body.max_per_slot is not None:
        updated["max_per_slot"] = body.max_per_slot
    updated.setdefault("slot_duration_minutes", DEFAULT_SLOT_DURATION)
    updated.setdefault("max_per_slot", DEFAULT_MAX_PER_SLOT)

    ad.booking_config = updated
    await db.commit()

    win_start, win_end = _campaign_window(ad)
    return BookingConfigOut(
        slot_duration_minutes=updated["slot_duration_minutes"],
        max_per_slot=updated["max_per_slot"],
        window_start=str(win_start),
        window_end=str(win_end),
    )
