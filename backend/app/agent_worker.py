# app/agent_worker.py
import os
from pathlib import Path

# --- load .env (both repo root and backend/.env are supported) ---
try:
    from dotenv import load_dotenv
    # Try repo root
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    # Try backend folder (where you're running the command)
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    # Fallback to current working dir
    load_dotenv(override=False)
except Exception:
    pass

from livekit.agents import AgentSession, Agent, JobContext, WorkerOptions, AutoSubscribe, cli
from livekit.plugins import openai as oai

SYSTEM_PROMPT = (
    "You are Arogya Hospital’s call assistant. Greet once. "
    "Then collect: (1) name, (2) purpose, (3) preferred date/time. "
    "Keep replies to 1–2 sentences. If asked for a human, confirm and stop speaking."
)

REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")
VOICE = os.getenv("OPENAI_VOICE", "alloy")

async def entrypoint(ctx: JobContext):
    # Tiny sanity check (won't print secrets)
    for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "OPENAI_API_KEY"):
        if not os.getenv(k):
            raise RuntimeError(f"Missing required environment variable: {k}")

    # Subscribe to audio only for lowest latency
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # OpenAI Realtime speech-to-speech
    session = AgentSession(
        llm=oai.realtime.RealtimeModel(
            model=REALTIME_MODEL,
            voice=VOICE,
            modalities=["audio", "text"],
        )
    )

    # Start the session/agent in this room
    await session.start(
        room=ctx.room,
        agent=Agent(instructions=SYSTEM_PROMPT),
    )

    # Greet once after the caller actually speaks (prevents greeting loops)
    greeted = {"sent": False}

    @session.on("user_input_transcribed")
    def _on_user_transcribed(ev):
        if not greeted["sent"] and getattr(ev, "is_final", False):
            greeted["sent"] = True
            ctx.create_task(session.generate_reply(
                instructions=("Hello! May I have your name, the reason for your call, "
                              "and a preferred date/time?")
            ))

    # Fallback: if nobody speaks for 4s, greet once
    async def _fallback():
        await ctx.sleep(4.0)
        if not greeted["sent"]:
            greeted["sent"] = True
            await session.generate_reply(
                instructions=("Hello! May I have your name, the reason for your call, "
                              "and a preferred date/time?")
            )
    ctx.create_task(_fallback())

if __name__ == "__main__":
    # The agent_name must match what you dispatch in main.py
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="arogya-mm-agent"))
