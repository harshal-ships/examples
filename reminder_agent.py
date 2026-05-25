"""Outbound HealthFirst appointment reminder agent.

This script runs the scheduled reminder side of the system:
- OpenClaw monitors bookings.json through this hourly loop.
- OpenClaw requests Telcoflow outbound calls for due reminders.
- Telcoflow and Gemini handle the live reminder conversation once the call exists.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from google import genai
from telcoflow_sdk import ActiveCall, TelcoflowClient, TelcoflowClientConfig
import telcoflow_sdk.events as events

from booking_agent import (
    BOOKINGS_PATH,
    OpenClawClient,
    TranscriptLine,
    ensure_google_calendar_credentials,
    make_gemini_client,
    make_telcoflow_config,
    run_gemini_voice_call,
    transcript_text,
)


# The loop interval is a constant so operators can see the scheduler cadence
# without hunting through the control flow.
CHECK_INTERVAL_SECONDS = 60 * 60


@dataclass(frozen=True)
class Booking:
    """Typed view of one bookings.json item used by the reminder scheduler."""

    id: str
    patient_name: str
    phone_number: str
    appointment_date: str
    appointment_time: str
    appointment_type: str
    status: str
    calendar_event_id: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Booking":
        return cls(
            id=str(data["id"]),
            patient_name=str(data["patient_name"]),
            phone_number=str(data["phone_number"]),
            appointment_date=str(data["appointment_date"]),
            appointment_time=str(data["appointment_time"]),
            appointment_type=str(data["appointment_type"]),
            status=str(data["status"]),
            calendar_event_id=str(data.get("calendar_event_id", "")),
        )

    @property
    def appointment_datetime(self) -> datetime:
        """Combine date and time fields for the 24-hour reminder check."""
        return datetime.fromisoformat(f"{self.appointment_date}T{self.appointment_time}")


class BookingStore:
    """Read-only booking loader; OpenClaw performs writes and status updates."""

    def __init__(self, path: Path = BOOKINGS_PATH):
        self.path = path

    def load(self) -> list[Booking]:
        """Return confirmed bookings from disk, treating a missing file as no work."""
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        bookings = data.get("bookings", [])
        if not isinstance(bookings, list):
            raise RuntimeError(f"{self.path} must contain a top-level bookings list.")
        return [Booking.from_json(item) for item in bookings if isinstance(item, dict)]


class ReminderCoordinator:
    """Coordinates hourly checks, OpenClaw outbound requests, and active calls."""

    def __init__(
        self,
        store: BookingStore,
        openclaw: OpenClawClient,
        gemini_client: genai.Client,
    ):
        self.store = store
        self.openclaw = openclaw
        self.gemini_client = gemini_client
        self.requested_booking_ids: set[str] = set()
        self.pending_calls_by_phone: dict[str, Booking] = {}

    async def hourly_loop(self) -> None:
        """Continuously scan bookings.json and request due reminder calls."""
        while True:
            try:
                await self.check_due_bookings()
            except Exception as exc:
                print(f"Reminder check failed: {exc}", file=sys.stderr)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def check_due_bookings(self) -> None:
        """Find confirmed appointments due in the next 24 hours."""
        now = datetime.now()
        due_before = now + timedelta(hours=24)

        for booking in self.store.load():
            if booking.status != "confirmed":
                continue
            if booking.id in self.requested_booking_ids:
                continue
            appointment_at = booking.appointment_datetime
            if now <= appointment_at <= due_before:
                await self.request_outbound_reminder(booking)

    async def request_outbound_reminder(self, booking: Booking) -> None:
        """Ask OpenClaw to trigger the Telcoflow outbound call for a due booking."""
        message = f"""
You are Maya's reminder automation worker for HealthFirst Clinic.

Use OpenClaw with the existing GOOGLE_API_KEY/GEMINI_API_KEY routing. OpenClaw has no API key of its own.
Use Telcoflow through the configured OpenClaw/Telcoflow capability to place an outbound reminder call.

Request:
- Call patient phone number: {booking.phone_number}
- Patient name: {booking.patient_name}
- Appointment: {booking.appointment_type} on {booking.appointment_date} at {booking.appointment_time}
- Booking id: {booking.id}

Return only JSON:
{{
  "telcoflow_outbound_requested": true | false,
  "phone_number": "{booking.phone_number}",
  "booking_id": "{booking.id}",
  "notes": "short operational note"
}}
""".strip()
        result = await self.openclaw.run_json(f"healthfirst-reminder-trigger-{booking.id}", message)
        if result.get("telcoflow_outbound_requested") is not True:
            raise RuntimeError(f"OpenClaw did not request Telcoflow outbound call: {result}")

        self.requested_booking_ids.add(booking.id)
        self.pending_calls_by_phone[booking.phone_number] = booking
        print(json.dumps({"outbound_request": result}, indent=2))

    async def handle_telcoflow_call(self, call: ActiveCall) -> None:
        """Attach Gemini to a reminder call once Telcoflow exposes the active call."""
        booking = self.match_booking_for_call(call)
        if booking is None:
            print(
                f"No pending reminder booking matched call {call.call_id}; disconnecting.",
                file=sys.stderr,
            )
            await call.disconnect()
            return

        prompt = make_reminder_prompt(booking)
        transcript = await run_gemini_voice_call(call, self.gemini_client, prompt)
        result = await self.process_reminder_transcript(booking, call, transcript)
        print(json.dumps({"call_id": call.call_id, "openclaw_result": result}, indent=2))
        self.pending_calls_by_phone.pop(booking.phone_number, None)

    def match_booking_for_call(self, call: ActiveCall) -> Booking | None:
        """Match either side of the Telcoflow call to the patient's E.164 number."""
        for phone in (call.caller_number, call.callee_number):
            if phone in self.pending_calls_by_phone:
                return self.pending_calls_by_phone[phone]
        return None

    async def process_reminder_transcript(
        self,
        booking: Booking,
        call: ActiveCall,
        transcript: list[TranscriptLine],
    ) -> dict[str, Any]:
        """Ask OpenClaw to update Calendar and bookings.json based on the reminder call."""
        rendered_transcript = transcript_text(transcript)
        if not rendered_transcript.strip():
            raise RuntimeError("Gemini did not return a transcript for OpenClaw to process.")

        message = f"""
You are Maya's post-reminder automation worker for HealthFirst Clinic.

Responsibilities:
- Use OpenClaw with existing GOOGLE_API_KEY/GEMINI_API_KEY routing. OpenClaw has no API key of its own.
- Use Google Calendar credentials from this path: {ensure_google_calendar_credentials()}
- Update only this booking database: {BOOKINGS_PATH}
- Use Google Calendar for all calendar changes.

Current booking:
{json.dumps(booking.__dict__, indent=2)}

Work to perform based on the transcript:
1. If the patient confirmed the appointment, update bookings.json status to "reminder_sent".
2. If the patient cancelled, delete/remove the Google Calendar event and update bookings.json status to "cancelled".
3. If the patient requested a reschedule, extract the new preferred date and time, update the Google Calendar event, and update bookings.json with the new date/time while keeping status "confirmed".
4. If the transcript is ambiguous, leave the booking unchanged and mark the result "needs_human_review".

Return only JSON:
{{
  "status": "reminder_sent" | "cancelled" | "rescheduled" | "needs_human_review",
  "booking_id": "{booking.id}",
  "calendar_event_id": "{booking.calendar_event_id}",
  "updated_booking": null | {{
    "id": "uuid",
    "patient_name": "string",
    "phone_number": "string",
    "appointment_date": "YYYY-MM-DD",
    "appointment_time": "HH:MM",
    "appointment_type": "general checkup|specialist|follow-up",
    "status": "confirmed|reminder_sent|cancelled",
    "calendar_event_id": "string"
  }},
  "notes": "short operational note"
}}

Telcoflow call metadata:
- call_id: {call.call_id}
- caller_number: {call.caller_number}
- callee_number: {call.callee_number}

Transcript:
{rendered_transcript}
""".strip()

        return await self.openclaw.run_json(f"healthfirst-reminder-result-{booking.id}", message)


def make_reminder_prompt(booking: Booking) -> str:
    """Build Maya's voice prompt for one patient reminder call."""
    return f"""Your name is Maya. You are calling from HealthFirst Clinic.
You are polite, concise, and helpful.
Greet the patient with: Hi, this is Maya calling from HealthFirst Clinic. I am calling to remind you about your appointment tomorrow. May I confirm you are {booking.patient_name}?
After confirming identity, remind them:
- Appointment type: {booking.appointment_type}
- Appointment date: {booking.appointment_date}
- Appointment time: {booking.appointment_time}
Ask whether they confirm, want to reschedule, or want to cancel.
If they want to reschedule, collect the new preferred date and time.
If they cancel, acknowledge calmly.
Before ending, clearly summarize what they chose."""


async def main() -> None:
    """Run Telcoflow call handling and the hourly reminder monitor together."""
    ensure_google_calendar_credentials()
    config: TelcoflowClientConfig = make_telcoflow_config()
    coordinator = ReminderCoordinator(
        store=BookingStore(),
        openclaw=OpenClawClient(),
        gemini_client=make_gemini_client(),
    )

    async with TelcoflowClient(config) as client:
        @client.on(events.INCOMING_CALL)
        async def on_call(call: ActiveCall) -> None:
            try:
                await coordinator.handle_telcoflow_call(call)
            except Exception as exc:
                print(f"Reminder call {call.call_id} failed: {exc}", file=sys.stderr)
                await call.disconnect()

        await asyncio.gather(client.run_forever(), coordinator.hourly_loop())


if __name__ == "__main__":
    asyncio.run(main())
