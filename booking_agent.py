"""Inbound HealthFirst appointment booking agent.

This script keeps the three responsibilities separate:
- Telcoflow owns phone-call audio in and out.
- Gemini owns the real-time voice conversation.
- OpenClaw extracts post-call booking details and sends patient confirmations.
- Python owns deterministic Calendar and bookings.json writes.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from google import genai
from google.genai import types
from google.oauth2 import service_account
from googleapiclient.discovery import build
from telcoflow_sdk import ActiveCall, TelcoflowClient, TelcoflowClientConfig
import telcoflow_sdk.events as events


try:
    from dotenv import load_dotenv
except ImportError:  # Render injects env vars directly; dotenv is only for local runs.
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


# Runtime constants are centralized so the Telcoflow, Gemini, and OpenClaw
# boundaries stay visible instead of being mixed into call handlers.
AUDIO_MIME_TYPE = "audio/pcm;rate=24000"
BOOKINGS_PATH = Path(os.getenv("BOOKINGS_PATH", "bookings.json")).resolve()
GEMINI_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
OPENCLAW_AGENT = os.getenv("OPENCLAW_AGENT", "main")
OPENCLAW_TIMEOUT_SECONDS = int(os.getenv("OPENCLAW_TIMEOUT_SECONDS", "900"))
LOG_TRANSCRIPTS = os.getenv("LOG_TRANSCRIPTS", "true").lower() in {"1", "true", "yes", "on"}
CLINIC_TIMEZONE = os.getenv("CLINIC_TIMEZONE", "Asia/Singapore")
APPOINTMENT_DURATION_MINUTES = int(os.getenv("APPOINTMENT_DURATION_MINUTES", "30"))
GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# Maya's system prompt is intentionally limited to the voice conversation.
# Calendar writes, messaging, and JSON persistence happen only after the call.
MAYA_BOOKING_PROMPT = """Your name is Maya. You are an appointment booking assistant for HealthFirst Clinic.
You are warm, calm, and efficient at all times.
You greet every caller with: Hi, thank you for calling HealthFirst Clinic. I am Maya, your appointment assistant. I can help you book, reschedule, or cancel an appointment. What would you like to do today?
Collect the following one step at a time:
- Patient full name
- Phone number
- Preferred appointment date
- Preferred appointment time
- Type of appointment: general checkup, specialist, or follow-up
Confirm all details only one time clearly before ending the call.
Do not claim that the appointment is booked during the call. Say that you will check availability and send a confirmation shortly."""


@dataclass(frozen=True)
class TranscriptLine:
    """Stores one transcript segment with speaker identity for OpenClaw review."""

    speaker: str
    text: str


@dataclass(frozen=True)
class AppointmentRange:
    """Timezone-aware appointment window used for Calendar free/busy checks."""

    start: datetime
    end: datetime


def require_env(name: str) -> str:
    """Fail fast when a required credential is missing so calls do not start half-configured."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def ensure_google_calendar_credentials() -> str:
    """Materialize Render's JSON secret into the credentials file OpenClaw expects."""
    credentials_json = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_JSON")
    if credentials_json:
        target_path = Path(
            os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH", "/data/google-calendar-credentials.json")
        ).expanduser().resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            credentials_data = json.loads(credentials_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "GOOGLE_CALENDAR_CREDENTIALS_JSON must be one complete JSON object, "
                "not separate Render env vars for each JSON field."
            ) from exc

        target_path.write_text(json.dumps(credentials_data), encoding="utf-8")
        target_path.chmod(0o600)
        os.environ["GOOGLE_CALENDAR_CREDENTIALS"] = str(target_path)
        return str(target_path)

    credentials_path = os.getenv("GOOGLE_CALENDAR_CREDENTIALS")
    if credentials_path:
        return credentials_path

    if not credentials_json:
        raise RuntimeError(
            "Missing Google Calendar credentials. Set GOOGLE_CALENDAR_CREDENTIALS "
            "to a credentials file path, or set GOOGLE_CALENDAR_CREDENTIALS_JSON "
            "to the full JSON credentials object."
        )


def make_gemini_client() -> genai.Client:
    """Create the Gemini client with the same Google key OpenClaw can route through."""
    return genai.Client(api_key=require_env("GOOGLE_API_KEY"))


def make_telcoflow_config() -> TelcoflowClientConfig:
    """Build the documented Telcoflow sandbox config for inbound media handling."""
    return TelcoflowClientConfig.sandbox(
        api_key=require_env("WSS_API_KEY"),
        connector_uuid=require_env("WSS_CONNECTOR_UUID"),
        sample_rate=24000,
    )


class GoogleCalendarClient:
    """Direct Google Calendar API client backed by the configured service account."""

    def __init__(
        self,
        credentials_path: str,
        calendar_id: str,
        timezone_name: str = CLINIC_TIMEZONE,
    ):
        self.calendar_id = calendar_id
        self.timezone_name = timezone_name
        self.timezone = ZoneInfo(timezone_name)
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=GOOGLE_CALENDAR_SCOPES,
        )
        self.service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def appointment_range(self, booking: dict[str, Any]) -> AppointmentRange:
        """Convert extracted date/time strings into a concrete appointment window."""
        appointment_date = str(booking["appointment_date"]).strip()
        appointment_time = str(booking["appointment_time"]).strip()
        if re.fullmatch(r"\d{2}:\d{2}", appointment_time):
            appointment_time = f"{appointment_time}:00"

        start = datetime.fromisoformat(f"{appointment_date}T{appointment_time}")
        if start.tzinfo is None:
            start = start.replace(tzinfo=self.timezone)
        end = start + timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
        return AppointmentRange(start=start, end=end)

    def is_available(self, appointment: AppointmentRange) -> bool:
        """Return True when the target calendar has no busy blocks in this window."""
        body = {
            "timeMin": appointment.start.isoformat(),
            "timeMax": appointment.end.isoformat(),
            "timeZone": self.timezone_name,
            "items": [{"id": self.calendar_id}],
        }
        response = self.service.freebusy().query(body=body).execute()
        busy_blocks = response.get("calendars", {}).get(self.calendar_id, {}).get("busy", [])
        return not busy_blocks

    def create_event(
        self,
        booking: dict[str, Any],
        appointment: AppointmentRange,
        call_id: str,
    ) -> dict[str, Any]:
        """Insert the confirmed appointment into the configured Google Calendar."""
        patient_name = str(booking["patient_name"]).strip()
        appointment_type = str(booking["appointment_type"]).strip()
        phone_number = str(booking["phone_number"]).strip()
        event = {
            "summary": f"HealthFirst {appointment_type} - {patient_name}",
            "description": (
                "Booked by Maya phone agent.\n"
                f"Patient: {patient_name}\n"
                f"Phone: {phone_number}\n"
                f"Appointment type: {appointment_type}\n"
                f"Telcoflow call id: {call_id}"
            ),
            "start": {
                "dateTime": appointment.start.isoformat(),
                "timeZone": self.timezone_name,
            },
            "end": {
                "dateTime": appointment.end.isoformat(),
                "timeZone": self.timezone_name,
            },
        }
        return self.service.events().insert(calendarId=self.calendar_id, body=event).execute()

    def next_available_slots(
        self,
        requested: AppointmentRange,
        count: int = 3,
    ) -> list[dict[str, str]]:
        """Find a small set of nearby alternatives during clinic hours."""
        slots: list[dict[str, str]] = []
        candidate_start = requested.start + timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
        clinic_open_hour = int(os.getenv("CLINIC_OPEN_HOUR", "9"))
        clinic_close_hour = int(os.getenv("CLINIC_CLOSE_HOUR", "17"))

        while len(slots) < count:
            if candidate_start.hour < clinic_open_hour:
                candidate_start = candidate_start.replace(
                    hour=clinic_open_hour, minute=0, second=0, microsecond=0
                )
            if candidate_start.hour >= clinic_close_hour:
                next_day = candidate_start + timedelta(days=1)
                candidate_start = next_day.replace(
                    hour=clinic_open_hour, minute=0, second=0, microsecond=0
                )

            candidate = AppointmentRange(
                start=candidate_start,
                end=candidate_start + timedelta(minutes=APPOINTMENT_DURATION_MINUTES),
            )
            if self.is_available(candidate):
                slots.append(
                    {
                        "appointment_date": candidate.start.date().isoformat(),
                        "appointment_time": candidate.start.strftime("%H:%M"),
                    }
                )
            candidate_start += timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

        return slots


def transcript_text(transcript: list[TranscriptLine]) -> str:
    """Render transcript lines as plain text so OpenClaw can reason over the full call."""
    return "\n".join(f"{line.speaker}: {line.text}" for line in transcript if line.text)


def record_transcript_line(transcript: list[TranscriptLine], speaker: str, text: str) -> None:
    """Store a transcript segment and optionally mirror it to container logs."""
    clean_text = text.strip()
    if not clean_text:
        return

    if transcript and transcript[-1].speaker == speaker:
        previous = transcript[-1].text
        separator = "" if clean_text[:1] in {".", ",", "!", "?", ";", ":"} else " "
        transcript[-1] = TranscriptLine(speaker, f"{previous}{separator}{clean_text}")
    else:
        transcript.append(TranscriptLine(speaker, clean_text))

    if LOG_TRANSCRIPTS:
        logger.info("Transcript [%s]: %s", speaker, clean_text)


async def run_gemini_voice_call(
    call: ActiveCall,
    gemini_client: genai.Client,
    system_prompt: str,
    initial_turn_text: str = "The phone call is connected. Begin the conversation now.",
) -> list[TranscriptLine]:
    """Bridge Telcoflow PCM audio to Gemini Live and return the captured transcript."""
    transcript: list[TranscriptLine] = []
    call_ended = asyncio.Event()

    @call.on(events.CALL_TERMINATED)
    def on_terminated() -> None:
        call_ended.set()

    await call.answer()

    # Live transcription lets OpenClaw receive a text transcript after the audio call ends.
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_prompt,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(
            language_code="en-US",
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
            ),
        ),
    )

    async with gemini_client.aio.live.connect(model=GEMINI_MODEL, config=config) as session:
        # System instructions shape behavior, but this first turn prompts Gemini to speak first.
        await session.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part(text=initial_turn_text)],
            ),
            turn_complete=True,
        )

        async def stream_to_gemini() -> None:
            """Forward each caller audio chunk to Gemini without transforming Telcoflow PCM."""
            async for chunk in call.audio_stream():
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type=AUDIO_MIME_TYPE)
                )

        async def receive_from_gemini() -> None:
            """Send Gemini audio back to Telcoflow across every conversation turn."""
            while not call_ended.is_set():
                async for response in session.receive():
                    content = response.server_content
                    if not content:
                        continue

                    if content.input_transcription and content.input_transcription.text:
                        record_transcript_line(
                            transcript, "PATIENT", content.input_transcription.text
                        )

                    if content.output_transcription and content.output_transcription.text:
                        record_transcript_line(
                            transcript, "MAYA", content.output_transcription.text
                        )

                    if content.interrupted:
                        if hasattr(call, "interrupt"):
                            await call.interrupt()
                        else:
                            await call.clear_send_audio_buffer()
                        break

                    if content.model_turn:
                        for part in content.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                await call.send_audio(part.inline_data.data)

        async def wait_for_call_end() -> None:
            """Stop the Gemini session promptly once Telcoflow reports the call is over."""
            await call_ended.wait()

        tasks = [
            asyncio.create_task(stream_to_gemini()),
            asyncio.create_task(receive_from_gemini()),
            asyncio.create_task(wait_for_call_end()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    return transcript


class OpenClawClient:
    """Small wrapper around the documented `openclaw agent` command."""

    def __init__(self, agent: str = OPENCLAW_AGENT, timeout: int = OPENCLAW_TIMEOUT_SECONDS):
        self.agent = agent
        self.timeout = timeout

    async def run_json(self, session_key: str, message: str) -> dict[str, Any]:
        """Run one OpenClaw turn and parse the JSON payload it is instructed to return."""
        return await asyncio.to_thread(self._run_json_sync, session_key, message)

    def _run_json_sync(self, session_key: str, message: str) -> dict[str, Any]:
        env = os.environ.copy()
        env["GEMINI_API_KEY"] = env.get("GEMINI_API_KEY") or require_env("GOOGLE_API_KEY")
        env["GOOGLE_API_KEY"] = require_env("GOOGLE_API_KEY")

        command = [
            "openclaw",
            "agent",
            "--agent",
            self.agent,
            "--local",
            "--session-key",
            session_key,
            "--message",
            message,
            "--json",
            "--timeout",
            str(self.timeout),
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            env=env,
            text=True,
            timeout=self.timeout + 30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"OpenClaw command failed with exit {result.returncode}: {result.stderr.strip()}"
            )

        return parse_openclaw_json(result.stdout)


def parse_openclaw_json(stdout: str) -> dict[str, Any]:
    """Handle OpenClaw JSON output and the agent's nested JSON response text."""
    outer = json.loads(stdout)
    candidate_texts: list[str] = []

    for container in (outer, outer.get("result") if isinstance(outer, dict) else None):
        if not isinstance(container, dict):
            continue
        for key in ("text", "message", "content", "output"):
            value = container.get(key)
            if isinstance(value, str):
                candidate_texts.append(value)
        payloads = container.get("payloads")
        if isinstance(payloads, list):
            for payload in payloads:
                if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                    candidate_texts.append(payload["text"])

    for text in candidate_texts:
        parsed = extract_json_object(text)
        if parsed is not None:
            return parsed

    if isinstance(outer, dict):
        return outer
    raise RuntimeError("OpenClaw returned JSON that could not be interpreted as an object.")


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a model response that may include prose."""
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    parsed = json.loads(match.group(0))
    return parsed if isinstance(parsed, dict) else None


async def process_booking_with_openclaw(
    openclaw: OpenClawClient,
    call: ActiveCall,
    transcript: list[TranscriptLine],
) -> dict[str, Any]:
    """Extract booking details with OpenClaw, then write Calendar/bookings directly."""
    calendar_credentials = ensure_google_calendar_credentials()
    calendar = GoogleCalendarClient(
        credentials_path=calendar_credentials,
        calendar_id=require_env("GOOGLE_CALENDAR_ID"),
    )
    rendered_transcript = transcript_text(transcript)
    if not rendered_transcript.strip():
        raise RuntimeError("Gemini did not return a transcript for OpenClaw to process.")

    logger.info("Call %s: extracting booking details with OpenClaw", call.call_id)
    extraction = await extract_booking_with_openclaw(openclaw, call, rendered_transcript)
    booking = normalize_extracted_booking(extraction, call)
    if booking is None:
        logger.info(
            "Call %s: booking needs human review. OpenClaw status=%s notes=%s",
            call.call_id,
            extraction.get("status"),
            extraction.get("notes"),
        )
        return {
            "status": "needs_human_review",
            "booking": None,
            "next_available_slots": [],
            "message_sent": False,
            "telcoflow_outbound_requested": False,
            "notes": extraction.get("notes", "OpenClaw could not extract a complete booking."),
        }

    appointment = calendar.appointment_range(booking)
    logger.info(
        "Call %s: checking calendar availability for %s to %s",
        call.call_id,
        appointment.start.isoformat(),
        appointment.end.isoformat(),
    )
    if not calendar.is_available(appointment):
        logger.info("Call %s: requested calendar slot is unavailable", call.call_id)
        return {
            "status": "unavailable",
            "booking": None,
            "next_available_slots": calendar.next_available_slots(appointment),
            "message_sent": False,
            "telcoflow_outbound_requested": False,
            "notes": "Requested slot is not available in Google Calendar.",
        }

    event = calendar.create_event(booking, appointment, call.call_id)
    logger.info("Call %s: created Google Calendar event %s", call.call_id, event.get("id"))
    booking_record = build_booking_record(booking, event)
    append_booking_record(booking_record)
    logger.info("Call %s: appended booking %s to %s", call.call_id, booking_record["id"], BOOKINGS_PATH)
    message_result = await send_confirmation_with_openclaw(openclaw, call, booking_record)

    return {
        "status": "confirmed",
        "booking": booking_record,
        "next_available_slots": [],
        "message_sent": message_result.get("message_sent") is True,
        "telcoflow_outbound_requested": False,
        "notes": message_result.get("notes", "Calendar event created and booking stored."),
    }


async def extract_booking_with_openclaw(
    openclaw: OpenClawClient,
    call: ActiveCall,
    rendered_transcript: str,
) -> dict[str, Any]:
    """Use OpenClaw only for structured extraction from the voice transcript."""
    today = datetime.now(ZoneInfo(CLINIC_TIMEZONE)).date().isoformat()
    message = f"""
You are Maya's post-call extraction worker for HealthFirst Clinic.

Responsibilities:
- Use OpenClaw with the existing GOOGLE_API_KEY/GEMINI_API_KEY routing. OpenClaw has no API key of its own.
- Extract structured appointment details from the transcript only.
- Do not check Google Calendar, create calendar events, edit files, or send messages.
- Today is {today}; resolve relative dates into YYYY-MM-DD.

Work to perform:
1. Read the full transcript below.
2. Extract patient full name, phone number, preferred date, preferred time, and appointment type.
3. If any required detail is missing or ambiguous, return "needs_human_review".

Return only one JSON object with:
{{
  "status": "extracted" | "needs_human_review",
  "booking": null | {{
    "patient_name": "string",
    "phone_number": "string",
    "appointment_date": "YYYY-MM-DD",
    "appointment_time": "HH:MM",
    "appointment_type": "general checkup|specialist|follow-up"
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

    return await openclaw.run_json(f"healthfirst-booking-extract-{call.call_id}", message)


def normalize_extracted_booking(
    extraction: dict[str, Any],
    call: ActiveCall,
) -> dict[str, str] | None:
    """Validate and normalize OpenClaw's extracted booking payload."""
    if extraction.get("status") not in {"extracted", "confirmed"}:
        return None
    booking = extraction.get("booking")
    if not isinstance(booking, dict):
        return None

    required_fields = [
        "patient_name",
        "phone_number",
        "appointment_date",
        "appointment_time",
        "appointment_type",
    ]
    normalized: dict[str, str] = {}
    for field in required_fields:
        value = str(booking.get(field, "")).strip()
        if not value or value.lower() in {"unknown", "null", "none"}:
            return None
        normalized[field] = value

    if normalized["phone_number"].lower() == "caller_number":
        normalized["phone_number"] = call.caller_number
    return normalized


def build_booking_record(booking: dict[str, str], event: dict[str, Any]) -> dict[str, str]:
    """Create the canonical bookings.json record after Calendar insertion succeeds."""
    return {
        "id": str(uuid.uuid4()),
        "patient_name": booking["patient_name"],
        "phone_number": booking["phone_number"],
        "appointment_date": booking["appointment_date"],
        "appointment_time": booking["appointment_time"][:5],
        "appointment_type": booking["appointment_type"],
        "status": "confirmed",
        "calendar_event_id": str(event["id"]),
    }


def append_booking_record(booking: dict[str, str]) -> None:
    """Persist a confirmed booking only after the Calendar event exists."""
    BOOKINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if BOOKINGS_PATH.exists():
        data = json.loads(BOOKINGS_PATH.read_text(encoding="utf-8"))
    else:
        data = {"bookings": []}

    bookings = data.get("bookings")
    if not isinstance(bookings, list):
        raise RuntimeError(f"{BOOKINGS_PATH} must contain a top-level bookings list.")

    bookings.append(booking)
    tmp_path = BOOKINGS_PATH.with_suffix(f"{BOOKINGS_PATH.suffix}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp_path.replace(BOOKINGS_PATH)


async def send_confirmation_with_openclaw(
    openclaw: OpenClawClient,
    call: ActiveCall,
    booking: dict[str, str],
) -> dict[str, Any]:
    """Ask OpenClaw only to send the patient confirmation via WhatsApp/Telegram."""
    message = f"""
You are Maya's patient confirmation worker for HealthFirst Clinic.

Responsibilities:
- Send one concise confirmation to the patient only through configured WhatsApp or Telegram.
- Prefer WhatsApp for the patient's phone number when available.
- If WhatsApp is unavailable but Telegram is configured, use Telegram.
- Never use SMS, Discord, Slack, email, or any other channel.
- Do not create, modify, or delete Google Calendar events.
- Do not edit bookings.json.

Confirmed booking:
{json.dumps(booking, indent=2)}

Telcoflow call metadata:
- call_id: {call.call_id}
- caller_number: {call.caller_number}
- callee_number: {call.callee_number}

Return only JSON:
{{
  "message_sent": true | false,
  "channel": "whatsapp" | "telegram" | "none",
  "notes": "short operational note"
}}
""".strip()
    try:
        return await openclaw.run_json(f"healthfirst-booking-confirm-{call.call_id}", message)
    except Exception as exc:
        logger.exception("OpenClaw confirmation send failed")
        return {
            "message_sent": False,
            "channel": "none",
            "notes": f"Calendar event created, but confirmation failed: {exc}",
        }


async def handle_incoming_call(
    call: ActiveCall,
    gemini_client: genai.Client,
    openclaw: OpenClawClient,
) -> None:
    """Run Maya's inbound booking call and hand the final transcript to OpenClaw."""
    transcript = await run_gemini_voice_call(call, gemini_client, MAYA_BOOKING_PROMPT)
    result = await process_booking_with_openclaw(openclaw, call, transcript)
    print(json.dumps({"call_id": call.call_id, "openclaw_result": result}, indent=2))


async def main() -> None:
    """Start the long-running Telcoflow client for inbound appointment calls."""
    ensure_google_calendar_credentials()
    gemini_client = make_gemini_client()
    openclaw = OpenClawClient()
    config = make_telcoflow_config()

    async with TelcoflowClient(config) as client:
        @client.on(events.INCOMING_CALL)
        async def on_call(call: ActiveCall) -> None:
            try:
                await handle_incoming_call(call, gemini_client, openclaw)
            except Exception as exc:
                print(f"Call {call.call_id} failed: {exc}", file=sys.stderr)
                await call.disconnect()

        await client.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
