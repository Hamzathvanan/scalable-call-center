from datetime import datetime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, DateTime, ForeignKey, Enum,  Text
import enum, uuid

class Base(DeclarativeBase): pass

class AgentStatus(str, enum.Enum):
    online="online"; offline="offline"; busy="busy"; on_break="break"

class Agent(Base):
    __tablename__="agents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[AgentStatus] = mapped_column(Enum(AgentStatus), default=AgentStatus.offline)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class CallStatus(str, enum.Enum):
    ringing="ringing"; assigned="assigned"; active="active"; ended="ended"

class Call(Base):
    __tablename__="calls"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    room_name: Mapped[str] = mapped_column(String(128), index=True)
    twilio_call_sid: Mapped[str|None] = mapped_column(String(64))
    caller_number: Mapped[str|None] = mapped_column(String(32))
    assigned_agent_id: Mapped[str|None] = mapped_column(ForeignKey("agents.id"))
    status: Mapped[CallStatus] = mapped_column(Enum(CallStatus), default=CallStatus.ringing)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
