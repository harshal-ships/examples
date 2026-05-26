# HealthFirst Voice Agents

A small example project showing how to build AI phone agents using Telcoflow, Gemini, OpenClaw, and Google Calendar.

This repo contains two Python agents:

* `booking_agent.py` → handles inbound appointment booking calls
* `reminder_agent.py` → handles outbound appointment reminder calls

The goal of the project is simple:

> Let patients call a clinic, talk naturally with an AI assistant, and automatically manage appointments.

The code keeps responsibilities separated so the system is easier to understand and extend.

* **Telcoflow** handles phone calls
* **Gemini** handles the live voice conversation
* **OpenClaw** extracts structured information after calls
* **Python** handles deterministic logic like calendar writes and JSON storage

---

## What the booking agent does

The inbound booking agent acts like a clinic receptionist.

When someone calls:

1. The AI answers the phone
2. It collects:

   * patient name
   * phone number
   * appointment date
   * appointment time
   * appointment type
3. The transcript is processed after the call
4. Availability is checked in Google Calendar
5. The booking is saved into `bookings.json`
6. A confirmation message can be sent to the patient

The assistant is intentionally limited during the live call.
It does not directly modify calendars or databases while talking.
That logic happens afterward in Python.

---

## What the reminder agent does

The reminder agent runs on a schedule.

Every hour it:

1. Reads `bookings.json`
2. Finds appointments happening in the next 24 hours
3. Requests outbound reminder calls
4. Starts an AI reminder conversation with the patient

This keeps reminder logic separate from booking logic.

---
