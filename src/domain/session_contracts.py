from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SessionPhase(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    RECORDING = "recording"
    SENDING = "sending"
    WAITING_RESPONSE = "waiting_response"
    PLAYING = "playing"
    COMPLETED = "completed"
    ERROR = "error"


SERVER_DRIVEN_EVENT_TYPES = frozenset(
    {
        "response.start",
        "response.end",
        "playback.start",
        "playback.end",
        "playback_file_start",
        "playback_file_done",
        "playback.queue_status",
        "playback_stopped",
        "stop_playback",
        "end_session",
        "playback_done",
        "final",
        "tts_complete",
        "audio_chunk",
    }
)


@dataclass
class SessionBinding:
    """Tracks the local client session id and the server-assigned session id."""

    client_session_id: str
    server_session_id: Optional[str] = None

    def bind(self, session_id: Optional[str]) -> Optional[str]:
        if session_id in {None, ""}:
            return self.server_session_id
        normalized = str(session_id)
        if normalized == self.client_session_id:
            return self.server_session_id or normalized
        if self.server_session_id is None:
            self.server_session_id = normalized
        return self.server_session_id

    def matches(self, session_id: Optional[str]) -> bool:
        if session_id in {None, ""}:
            return True
        normalized = str(session_id)
        if normalized == self.client_session_id:
            return True
        if self.server_session_id is None:
            return False
        return normalized == self.server_session_id

    @property
    def active_session_id(self) -> str:
        return self.server_session_id or self.client_session_id

    @property
    def is_server_bound(self) -> bool:
        return self.server_session_id is not None
