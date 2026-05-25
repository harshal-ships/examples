"""Inbound HealthFirst appointment booking agent.

This script keeps the three responsibilities separate:
- Telcoflow owns phone-call audio in and out.
- Gemini owns the real-time voice conversation.
- OpenClaw owns post-call automation across Calendar, messaging, and bookings.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from telcoflow_sdk import ActiveCall, TelcoflowClient, TelcoflowClientConfig
import telcoflow_sdk.events as events


# Runtime constants are centralized so the Telcoflow, Gemini, and OpenClaw
# boundaries stay visible instead of being mixed into call handlers.
AUDIO_MIME_TYPE = "audio/pcm;rate=24000"
BOOKINGS_PATH = Path("bookings.json").resolve()
GEMINI_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
OPENCLAW_AGENT = os.getenv("OPENCLAW_AGENT", "main")
OPENCLAW_TIMEOUT_SECONDS = int(os.getenv("OPENCLAW_TIMEOUT_SECONDS", "900"))


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
Confirm all details clearly before ending the call.
Do not claim that the appointment is booked during the call. Say that you will check availability and send a confirmation shortly."""


@dataclass(frozen=True)
class TranscriptLine:
    """Stores one transcript segment with speaker identity for OpenClaw review."""

    speaker: str
    text: str


def require_env(name: str) -> str:
    """Fail fast when a required credential is missing so calls do not start half-configured."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


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


def transcript_text(transcript: list[TranscriptLine]) -> str:
    """Render transcript lines as plain text so OpenClaw can reason over the full call."""
    return "\n".join(f"{line.speaker}: {line.text}" for line in transcript if line.text)


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
            """Send Gemini audio back to Telcoflow and collect both sides' transcripts."""
            async for response in session.receive():
                content = response.server_content
                if not content:
                    continue

                if content.input_transcription and content.input_transcription.text:
                    transcript.append(
                        TranscriptLine("PATIENT", content.input_transcription.text.strip())
                    )

                if content.output_transcription and content.output_transcription.text:
                    transcript.append(
                        TranscriptLine("MAYA", content.output_transcription.text.strip())
                    )

                if content.interrupted:
                    await call.clear_send_audio_buffer()

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
    """Ask OpenClaw to extract booking details and perform all external actions."""
    calendar_credentials = require_env("GOOGLE_CALENDAR_CREDENTIALS")
    rendered_transcript = transcript_text(transcript)
    if not rendered_transcript.strip():
        raise RuntimeError("Gemini did not return a transcript for OpenClaw to process.")

    message = f"""
You are Maya's post-call automation worker for HealthFirst Clinic.

Responsibilities:
- Use OpenClaw with the existing GOOGLE_API_KEY/GEMINI_API_KEY routing. OpenClaw has no API key of its own.
- Use Google Calendar credentials from this path: {calendar_credentials}
- Treat {BOOKINGS_PATH} as the booking database with this shape:
  {{"bookings":[{{"id":"uuid","patient_name":"John Doe","phone_number":"+6512345678","appointment_date":"2026-05-28","appointment_time":"10:00","appointment_type":"general checkup","status":"confirmed","calendar_event_id":"google_calendar_event_id"}}]}}
- Do not modify unrelated files.

Work to perform:
1. Read the full transcript below.
2. Extract patient full name, phone number, preferred date, preferred time, and appointment type.
3. Check Google Calendar availability for the requested HealthFirst Clinic appointment slot.
4. If available, create the Google Calendar event, append the booking to bookings.json with status "confirmed", and send the patient a WhatsApp confirmation. If WhatsApp is not configured, send SMS through the configured messaging channel.
5. If unavailable, find the next 3 available appointment slots and use OpenClaw to trigger a Telcoflow outbound call to the patient's phone number so Maya can offer those slots. Also send those options by WhatsApp or SMS.

Return only one JSON object with:
{{
  "status": "confirmed" | "unavailable" | "needs_human_review",
  "booking": null | {{
    "id": "uuid",
    "patient_name": "string",
    "phone_number": "string",
    "appointment_date": "YYYY-MM-DD",
    "appointment_time": "HH:MM",
    "appointment_type": "general checkup|specialist|follow-up",
    "status": "confirmed",
    "calendar_event_id": "string"
  }},
  "next_available_slots": [
    {{"appointment_date": "YYYY-MM-DD", "appointment_time": "HH:MM"}}
  ],
  "message_sent": true | false,
  "telcoflow_outbound_requested": true | false,
  "notes": "short operational note"
}}

Telcoflow call metadata:
- call_id: {call.call_id}
- caller_number: {call.caller_number}
- callee_number: {call.callee_number}

Transcript:
{rendered_transcript}
""".strip()

    return await openclaw.run_json(f"healthfirst-booking-{call.call_id}", message)


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
    require_env("GOOGLE_CALENDAR_CREDENTIALS")
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
