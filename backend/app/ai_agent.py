import asyncio
import os
import subprocess
import tempfile
from typing import List, Dict, Tuple, Optional

import numpy as np
import soundfile as sf
from livekit import api as lk_api
from livekit import rtc
from livekit.rtc.audio_stream import AudioStream
from openai import OpenAI

LK_URL = os.getenv("LIVEKIT_URL")
LK_KEY = os.getenv("LIVEKIT_API_KEY")
LK_SECRET = os.getenv("LIVEKIT_API_SECRET")

client = OpenAI()

# ---------- helpers ----------
async def _ensure_wav_bytes(raw_bytes: bytes) -> Tuple[np.ndarray, int, int]:
    with tempfile.NamedTemporaryFile(suffix=".blob", delete=False) as f:
        f.write(raw_bytes)
        in_path = f.name
    try:
        data, sr = sf.read(in_path, dtype="int16", always_2d=True)
        return data, sr, data.shape[1]
    except RuntimeError:
        pass
    wav_path = in_path + ".wav"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", in_path, wav_path],
        check=True,
    )
    data, sr = sf.read(wav_path, dtype="int16", always_2d=True)
    return data, sr, data.shape[1]

async def _write_wav_from_pcm16(pcm_bytes: bytes, sample_rate: int, channels: int) -> str:
    arr = np.frombuffer(pcm_bytes, dtype=np.int16).reshape((-1, max(1, channels)))
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        sf.write(f.name, arr, samplerate=sample_rate, subtype="PCM_16")
        return f.name

# ---------- TTS / STT / LLM ----------
async def speak(room: rtc.Room, text: str) -> None:
    try:
        speech = client.audio.speech.create(
            model=os.getenv("OPENAI_TTS_MODEL", "tts-1"),
            voice=os.getenv("OPENAI_VOICE", "alloy"),
            input=text,
            response_format="wav",
        )
    except TypeError:
        speech = client.audio.speech.create(
            model=os.getenv("OPENAI_TTS_MODEL", "tts-1"),
            voice=os.getenv("OPENAI_VOICE", "alloy"),
            input=text,
        )

    data, sr, ch = await _ensure_wav_bytes(speech.read())
    source = rtc.AudioSource(sample_rate=sr, num_channels=ch)
    track = rtc.LocalAudioTrack.create_audio_track("agent-tts", source)
    pub = await room.local_participant.publish_track(track)

    samples_per_chunk = int(sr * 0.02)  # 20 ms
    i = 0
    while i < len(data):
        chunk = data[i:i + samples_per_chunk]
        if chunk.size == 0:
            break
        frame = rtc.AudioFrame(
            data=chunk.tobytes(),
            sample_rate=sr,
            num_channels=ch,
            samples_per_channel=len(chunk),
        )
        await source.capture_frame(frame)
        await asyncio.sleep(0.02)
        i += samples_per_chunk

    try:
        result = room.local_participant.unpublish_track(pub.sid)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass

async def transcribe_pcm_chunk(pcm_bytes: bytes, sample_rate: int, channels: int) -> str:
    wav_path = await _write_wav_from_pcm16(pcm_bytes, sample_rate, channels)
    tr = client.audio.transcriptions.create(
        model=os.getenv("OPENAI_STT_MODEL", "whisper-1"),
        file=open(wav_path, "rb"),
    )
    return (tr.text or "").strip()

async def converse(prompt: str, history: List[Dict[str, str]]) -> str:
    msgs = [
        {"role": "system", "content": (
            "You are Arogya hospital’s helpful call assistant. "
            "Greet, collect name, purpose, and preferred date/time. "
            "Keep responses to 1–2 sentences."
        )},
        *history,
        {"role": "user", "content": prompt},
    ]
    r = client.chat.completions.create(
        model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
        messages=msgs,
        temperature=0.3,
    )
    return r.choices[0].message.content

# ---------- loop over remote SIP audio ----------
async def stt_turn_loop(room: rtc.Room, remote_track: rtc.RemoteAudioTrack) -> None:
    astream = AudioStream.from_track(track=remote_track, sample_rate=16000, num_channels=1)

    history: List[Dict[str, str]] = []
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    window_ms = 3000
    pcm_buf = bytearray()

    try:
        async for ev in astream:
            frame = ev.frame
            if sample_rate is None:
                sample_rate = frame.sample_rate
                channels = frame.num_channels

            pcm_buf.extend(frame.data)
            duration_ms = int(1000 * frame.samples_per_channel / frame.sample_rate)
            window_ms -= duration_ms

            if window_ms > 0:
                continue

            window_ms = 3000
            text = await transcribe_pcm_chunk(bytes(pcm_buf), sample_rate, channels)
            pcm_buf.clear()
            if not text:
                continue

            print(f"[AI] Heard: {text}")
            if any(k in text.lower() for k in ("agent", "human", "representative", "operator")):
                await speak(room, "Okay, connecting you to a human agent.")
                break

            reply = await converse(text, history)
            history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
            print(f"[AI] Reply: {reply}")
            await speak(room, reply)
    finally:
        await astream.aclose()

# ---------- main entry ----------
async def run_ai_agent(room_name: str) -> None:
    identity = f"ai-agent-{room_name}"
    token = (
        lk_api.AccessToken(api_key=LK_KEY, api_secret=LK_SECRET)
        .with_identity(identity)
        .with_name("AI Assistant")
        .with_grants(lk_api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )

    room = rtc.Room()
    try:
        room.on_connection_state_changed(lambda s: print(f"[AI] state={s} room={room_name}"))
    except Exception:
        pass

    print(f"[AI] connecting… url={LK_URL} identity={identity} room={room_name}")
    await room.connect(LK_URL, token)
    print("[AI] joined", room_name)

    started = asyncio.Event()

    async def _start_if_audio_track(track, participant):
        if started.is_set():
            return
        if isinstance(track, rtc.RemoteAudioTrack):
            print(f"[AI] remote audio subscribed from participant={getattr(participant, 'identity', '')}")
            started.set()
            asyncio.create_task(stt_turn_loop(room, track))

    async def _subscribe_pub(pub, participant):
        kind = getattr(pub, "kind", None) or getattr(getattr(pub, "track", None), "kind", None)
        if kind not in (getattr(rtc.TrackKind, "KIND_AUDIO", None), "audio"):
            return

        if getattr(pub, "subscribed", False):
            track = getattr(pub, "track", None)
            if track:
                await _start_if_audio_track(track, participant)
            return

        sid = getattr(pub, "sid", "?")
        print(f"[AI] subscribing to publication sid={sid} of {getattr(participant, 'identity', '')}")
        try:
            result = pub.set_subscribed(True)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            print("[AI] set_subscribed error:", e)
            return

        try:
            pub.on_subscribed(lambda track: asyncio.create_task(_start_if_audio_track(track, participant)))
        except Exception:
            track = getattr(pub, "track", None)
            if track:
                await _start_if_audio_track(track, participant)

    async def _sweep_and_subscribe_all():
        rps = getattr(room, "remote_participants", {})
        rp_values = list(rps.values()) if isinstance(rps, dict) else list(rps or [])
        if not rp_values:
            print("[AI] sweep: no remote participants yet")
            return
        for rp in rp_values:
            pubs = getattr(rp, "track_publications", {})
            pubs = pubs.values() if isinstance(pubs, dict) else pubs
            for pub in pubs or []:
                await _subscribe_pub(pub, rp)

    try:
        def _on_track_published(pub, participant):
            asyncio.create_task(_subscribe_pub(pub, participant))
        off_published = room.on_track_published(_on_track_published)
    except Exception:
        def off_published(): pass

    try:
        def _on_participant_connected(participant):
            asyncio.create_task(_sweep_and_subscribe_all())
        off_participant = room.on_participant_connected(_on_participant_connected)
    except Exception:
        def off_participant(): pass

    for _ in range(5):
        await _sweep_and_subscribe_all()
        if started.is_set():
            break
        await asyncio.sleep(1.0)

    async def _background_sweeper():
        for _ in range(20):
            if started.is_set():
                return
            await _sweep_and_subscribe_all()
            await asyncio.sleep(1.0)
        if not started.is_set():
            print("[AI][WARN] no remote audio within 20s — check SIP ingress & permissions.")
    asyncio.create_task(_background_sweeper())

    await speak(room, "Hello! This is Arogya's automated assistant. How can I help you today?")

    done = asyncio.Event()
    try:
        room.on_disconnected(lambda *_: done.set())
    except Exception:
        pass
    await done.wait()
    off_published()
    off_participant()
    await room.disconnect()
