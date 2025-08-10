// web/agent-web/src/App.jsx
import {useEffect, useRef, useState} from "react";
import axios from "axios";
import {LiveKitRoom, useTracks, TrackLoop, RoomAudioRenderer, useLocalParticipant} from "@livekit/components-react";
import "@livekit/components-styles";
import {Track} from "livekit-client";


const API = "http://localhost:8000";

const serverUrl = import.meta.env.VITE_LIVEKIT_URL;
console.log('LiveKit serverUrl =', serverUrl);

function AudioSubscriber() {
    // play all remote audio tracks
    return (
        <TrackLoop tracks={useTracks([Track.Source.Microphone])}>
            {(trackPublication) => <audio data-lk-local={false} autoPlay/>}
        </TrackLoop>
    );
}

export default function App() {
    const [room, setRoom] = useState(null);
    const [token, setToken] = useState(null);
    const [agentId, setAgentId] = useState(null);
    const hasRegistered = useRef(false);

    const register = async () => {
        const r = await axios.post(`${API}/agents/register`, {
            username: "hamza",
            full_name: "Hamza Agent",
        });
        setAgentId(r.data.agent_id);
    };

    const getNextCall = async () => {
        const r = await axios.get(`${API}/calls/next`);
        if (r.data.room) setRoom(r.data.room);
    };

    const getToken = async () => {
        const r = await axios.post(`${API}/livekit/token`, {agent_id: agentId, room});
        setToken(r.data.token);
    };

    useEffect(() => {
        if (hasRegistered.current) return;   // prevent second run in Strict Mode
        hasRegistered.current = true;
        register();
    }, []);

    return (
        <div style={{padding: 24}}>
            <h2>Agent Console</h2>
            <p>Agent: {agentId || "Registering..."}</p>
            {!room && <button onClick={getNextCall}>Fetch next ringing call</button>}
            {room && !token && <button onClick={getToken}>Answer call (join {room})</button>}
            {token && (
                <LiveKitRoom
                    token={token}
                    serverUrl={serverUrl}
                    connect={true}
                    audio={true}
                    video={false}
                    onConnected={() => console.log('Connected to LiveKit')}
                    onDisconnected={() => console.log('Disconnected from LiveKit')}
                    onError={(e) => console.error('LK error', e)}
                    style={{height: '60vh', border: '1px solid #eee', marginTop: 16}}
                >
                    <RoomAudioRenderer/>
                    <div style={{marginTop: 12}}>
                        <p>Connected to {room}. You’re live.</p>
                    </div>
                </LiveKitRoom>
            )}
        </div>
    );
}
