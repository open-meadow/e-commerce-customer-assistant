"""Inline LiveKit browser component for voice calls and transcripts."""

from __future__ import annotations

import streamlit as st

_LIVEKIT_CLIENT_URL = (
    "https://cdn.jsdelivr.net/npm/livekit-client@2.24.0/+esm"
)

_LIVEKIT_HTML = """
<section class="voice-call" aria-label="LiveKit voice call">
  <button class="connect-button" type="button">Connect microphone</button>
  <div class="connection-status" role="status" aria-live="polite">
    Voice session ready. Connect your microphone to begin.
  </div>
  <div
    class="transcript"
    role="log"
    aria-live="polite"
    aria-label="Voice conversation transcript"
  ></div>
  <div class="audio-output"></div>
</section>
"""

_LIVEKIT_CSS = """
.voice-call {
  display: grid;
  gap: 0.75rem;
  color: var(--st-text-color);
  font-family: var(--st-font, system-ui, sans-serif);
}
.connect-button {
  width: fit-content;
  border: 1px solid var(--st-primary-color);
  border-radius: var(--st-button-radius, 0.5rem);
  padding: 0.5rem 0.9rem;
  color: var(--st-primary-color);
  background: var(--st-background-color);
  font: inherit;
  cursor: pointer;
}
.connect-button:disabled {
  cursor: wait;
  opacity: 0.65;
}
.connection-status {
  font-size: 0.9rem;
  color: var(--st-secondary-text-color);
}
.transcript {
  display: grid;
  gap: 0.5rem;
  max-height: 16rem;
  overflow-y: auto;
  padding: 0.25rem;
}
.transcript-line {
  overflow-wrap: anywhere;
  padding: 0.5rem 0.65rem;
  border-radius: 0.5rem;
  background: var(--st-secondary-background-color);
}
.transcript-speaker {
  display: block;
  margin-bottom: 0.15rem;
  font-size: 0.75rem;
  color: var(--st-secondary-text-color);
}
.audio-output {
  display: none;
}
"""

_LIVEKIT_JS = f"""
const sdkUrl = "{_LIVEKIT_CLIENT_URL}";
const instances = new WeakMap();

function setStatus(state, text) {{
  state.status.textContent = text;
}}

function addTranscriptLine(state, segmentId, speaker, text, isFinal) {{
  let line = state.segments.get(segmentId);
  if (!line) {{
    line = document.createElement("div");
    line.className = "transcript-line";
    const speakerLabel = document.createElement("span");
    speakerLabel.className = "transcript-speaker";
    const transcriptText = document.createElement("span");
    line.append(speakerLabel, transcriptText);
    state.transcript.appendChild(line);
    state.segments.set(segmentId, {{ line, speakerLabel, transcriptText }});
  }}
  const entry = state.segments.get(segmentId);
  entry.speakerLabel.textContent = speaker;
  entry.transcriptText.textContent = text;
  if (isFinal) state.segments.delete(segmentId);
  state.transcript.scrollTop = state.transcript.scrollHeight;
}}

async function connect(state) {{
  if (state.connecting || state.room) return;
  state.connecting = true;
  state.button.disabled = true;
  setStatus(state, "Connecting and requesting microphone access...");

  try {{
    const {{ Room, RoomEvent, Track }} = await import(sdkUrl);
    const room = new Room({{ adaptiveStream: true, dynacast: true }});
    state.room = room;

    room.registerTextStreamHandler(
      "lk.transcription",
      async (reader, participantInfo) => {{
        const attrs = reader.info?.attributes || {{}};
        const segmentId =
          attrs["lk.segment_id"] || reader.info?.id || `${{Date.now()}}-${{Math.random()}}`;
        const isFinal = attrs["lk.transcription_final"] === "true";
        const isLocal =
          participantInfo.identity === room.localParticipant.identity;
        let text = "";
        for await (const chunk of reader) text += chunk;
        text = text.trim();
        if (text) {{
          addTranscriptLine(
            state,
            segmentId,
            isLocal ? "You" : "Assistant",
            text,
            isFinal,
          );
        }}
      }},
    );

    room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {{
      if (
        participant.identity !== room.localParticipant.identity &&
        track.kind === Track.Kind.Audio
      ) {{
        const audio = track.attach();
        audio.autoplay = true;
        audio.playsInline = true;
        state.audio.appendChild(audio);
        audio.play().catch(() => {{
          setStatus(
            state,
            "Connected. Allow audio playback in your browser to hear replies.",
          );
        }});
      }}
    }});

    room.on(RoomEvent.Disconnected, () => {{
      if (state.room !== room) return;
      state.room = null;
      state.button.disabled = false;
      state.button.textContent = "Reconnect microphone";
      setStatus(state, "Disconnected. Reconnect or end the voice call.");
      state.audio.replaceChildren();
    }});

    await room.connect(state.data.url, state.data.token);
    await room.localParticipant.setMicrophoneEnabled(true);
    state.button.textContent = "Microphone connected";
    setStatus(state, "Connected. Speak naturally; your conversation appears below.");
  }} catch (error) {{
    const room = state.room;
    state.room = null;
    if (room) room.disconnect();
    state.button.disabled = false;
    state.button.textContent = "Try connecting again";
    setStatus(state, "Could not connect. Check microphone permission and try again.");
  }} finally {{
    state.connecting = false;
  }}
}}

export default function (component) {{
  const {{ data, parentElement }} = component;
  let state = instances.get(parentElement);
  if (!state) {{
    state = {{
      button: parentElement.querySelector(".connect-button"),
      status: parentElement.querySelector(".connection-status"),
      transcript: parentElement.querySelector(".transcript"),
      audio: parentElement.querySelector(".audio-output"),
      segments: new Map(),
      room: null,
      connecting: false,
      data,
    }};
    state.button.onclick = () => connect(state);
    instances.set(parentElement, state);
  }} else {{
    state.data = data;
  }}

  return () => {{
    const room = state.room;
    state.room = null;
    if (room) room.disconnect();
    instances.delete(parentElement);
  }};
}}
"""

_LIVEKIT_COMPONENT = st.components.v2.component(
    "livekit_voice_call",
    html=_LIVEKIT_HTML,
    css=_LIVEKIT_CSS,
    js=_LIVEKIT_JS,
)


def livekit_voice_call(url: str, token: str, *, key: str) -> None:
    _LIVEKIT_COMPONENT(
        data={"url": url, "token": token},
        key=key,
        width="stretch",
        height="content",
    )
