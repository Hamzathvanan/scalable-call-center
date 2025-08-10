import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import DateTime, select
from sqlalchemy.orm import Session, Mapped, mapped_column
from sqlalchemy.exc import IntegrityError
from starlette.requests import ClientDisconnect

from .models import Base, Agent, AgentStatus, Call, CallStatus
from .deps import SessionLocal, make_lk_token, engine
from livekit import api as lk_api

# ---------- FastAPI lifespan (replaces @on_event) ----------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # startup
    Base.metadata.create_all(bind=engine)
    yield
    # shutdown: nothing to clean up in this variant

app = FastAPI(title="Call Center Backend", lifespan=lifespan)

updated_at: Mapped[datetime] = mapped_column(
    DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
)

origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

active_ai_tasks: dict[str, asyncio.Task] = {}

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# -------- Schemas --------
class AgentCreate(BaseModel):
    username: str
    full_name: str

class TokenRequest(BaseModel):
    agent_id: str
    room: str

# -------- Routes --------
@app.post("/agents/register")
def register_agent(payload: AgentCreate, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.username == payload.username).first()
    now = datetime.now(timezone.utc)

    if agent:
        agent.full_name = payload.full_name or agent.full_name
        agent.status = AgentStatus.online
        agent.updated_at = now
        db.commit()
        db.refresh(agent)
        return {"agent_id": agent.id, "status": agent.status, "username": agent.username}

    a = Agent(username=payload.username, full_name=payload.full_name,
              status=AgentStatus.online, created_at=now, updated_at=now)
    db.add(a)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        agent = db.query(Agent).filter(Agent.username == payload.username).first()
        if not agent:
            raise
        agent.full_name = payload.full_name or agent.full_name
        agent.status = AgentStatus.online
        agent.updated_at = now
        db.commit()
        db.refresh(agent)
        return {"agent_id": agent.id, "status": agent.status, "username": agent.username}

    db.refresh(a)
    return {"agent_id": a.id, "status": a.status, "username": a.username}

@app.post("/livekit/token")
def get_token(req: TokenRequest, db: Session = Depends(get_db)):
    agent = db.get(Agent, req.agent_id)
    token = make_lk_token(identity=agent.id, name=agent.full_name, room=req.room)
    return {"token": token}

@app.get("/calls/next")
def next_call(db: Session = Depends(get_db)):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
    call = (
        db.query(Call)
        .filter(Call.status == CallStatus.ringing, Call.created_at >= cutoff)
        .order_by(Call.created_at)
        .first()
    )
    return {"room": call.room_name, "call_id": call.id} if call else {"room": None}

# -------- LiveKit webhook --------
@app.post("/webhooks/livekit")
async def livekit_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.body()
        if not body:
            return {"ok": True}
        payload = json.loads(body.decode("utf-8"))
    except ClientDisconnect:
        return {"ok": True}
    except Exception as e:
        print("[WEBHOOK PARSE ERROR]", e)
        return {"ok": True}

    print("[LK WEBHOOK]", payload)

    evt = payload.get("event")
    data = payload.get("data") or payload
    room = (payload.get("room") or {}).get("name")
    part = (payload.get("participant") or {})
    kind = part.get("kind")

    def upsert_call(room_name: str, twilio_sid: Optional[str] = None, caller: Optional[str] = None):
        call = db.execute(select(Call).where(Call.room_name == room_name)).scalar_one_or_none()
        if call:
            changed = False
            if twilio_sid and not call.twilio_call_sid:
                call.twilio_call_sid = twilio_sid; changed = True
            if caller and not call.caller_number:
                call.caller_number = caller; changed = True
            if call.status == CallStatus.ended:
                call.status = CallStatus.ringing; changed = True
            if changed:
                db.commit()
            return call

        new_call = Call(
            room_name=room_name,
            twilio_call_sid=twilio_sid,
            caller_number=caller,
            status=CallStatus.ringing,
            created_at=datetime.now(timezone.utc),
        )
        db.add(new_call); db.commit()
        return new_call

    room_name = None
    twilio_sid = None
    caller = None

    if evt in ("room_started", "room_finished") and "room" in data:
        room_name = data["room"].get("name")

    if evt == "participant_joined":
        room_name = data.get("room", {}).get("name") or room_name
        p = data.get("participant", {}) or {}
        attrs = p.get("attributes", {}) or {}
        twilio_sid = attrs.get("sip.twilio.callSid") or attrs.get("twilioCallSid")
        caller = attrs.get("sip.phoneNumber") or attrs.get("caller") or attrs.get("from")

    if evt == "ingress_started":
        ig = data.get("ingress", {}) or {}
        room_name = ig.get("roomName") or room_name
        meta = ig.get("metadata") or {}
        caller = meta.get("from") or caller

    if evt in ("room_started", "participant_joined", "ingress_started") and room_name:
        upsert_call(room_name, twilio_sid, caller)

    # Start the AI once when the SIP leg joins
    if evt == "participant_joined" and kind == "SIP" and room:
        print(f"[DISPATCH] requesting agent for room {room}")
        try:
            lkapi = lk_api.LiveKitAPI()  # uses LIVEKIT_* envs
            await lkapi.agent_dispatch.create_dispatch(
                lk_api.CreateAgentDispatchRequest(
                    agent_name="arogya-mm-agent",  # must match worker's WorkerOptions.agent_name
                    room=room,
                    # pass any context you want the agent to see:
                    metadata=json.dumps({
                        "source": "inbound_sip",
                        "caller": caller,
                        "twilio_sid": twilio_sid,
                    }),
                )
            )
            await lkapi.aclose()
        except Exception as e:
            print("[DISPATCH][ERROR]", e)

    # Cleanup
    if evt in ("room_finished", "ingress_ended") and room:
        active_ai_tasks.pop(room, None)
        call_row = db.execute(select(Call).where(Call.room_name == room)).scalar_one_or_none()
        if call_row:
            call_row.status = CallStatus.ended
            db.commit()

    if evt == "participant_left":
        p = data.get("participant", {}) or {}
        if p.get("kind") == "SIP":
            room_name = data.get("room", {}).get("name")
            if room_name:
                call = db.execute(select(Call).where(Call.room_name == room_name)).scalar_one_or_none()
                if call and call.status != CallStatus.ended:
                    call.status = CallStatus.ended
                    db.commit()

    if evt == "track_unpublished":
        p = data.get("participant", {}) or {}
        if p.get("kind") == "SIP":
            room_name = data.get("room", {}).get("name")
            if room_name:
                call = db.execute(select(Call).where(Call.room_name == room_name)).scalar_one_or_none()
                if call and call.status != CallStatus.ended:
                    call.status = CallStatus.ended
                    db.commit()

    return {"ok": True}
