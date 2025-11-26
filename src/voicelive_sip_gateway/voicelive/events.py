"""Domain-level events mirrored from the Azure Voice Live SDK."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict


class VoiceLiveEventType(Enum):
    """Subset of server events the gateway currently cares about."""

    SESSION_CREATED = auto()
    SESSION_UPDATED = auto()
    CONVERSATION_ITEM_CREATED = auto()
    INPUT_AUDIO_BUFFER_COMMITTED = auto()
    INPUT_AUDIO_BUFFER_SPEECH_STARTED = auto()
    INPUT_AUDIO_BUFFER_SPEECH_STOPPED = auto()
    INPUT_AUDIO_TRANSCRIPTION_DELTA = auto()
    INPUT_AUDIO_TRANSCRIPTION_COMPLETED = auto()
    RESPONSE_CREATED = auto()
    RESPONSE_OUTPUT_ITEM_ADDED = auto()
    RESPONSE_OUTPUT_ITEM_DONE = auto()
    RESPONSE_CONTENT_PART_ADDED = auto()
    RESPONSE_CONTENT_PART_DONE = auto()
    RESPONSE_AUDIO_DELTA = auto()
    RESPONSE_AUDIO_DONE = auto()
    RESPONSE_AUDIO_TRANSCRIPT_DELTA = auto()
    RESPONSE_AUDIO_TRANSCRIPT_DONE = auto()
    RESPONSE_DONE = auto()
    ERROR = auto()


@dataclass(slots=True)
class VoiceLiveEvent:
    """Lightweight representation of a Voice Live server event."""

    type: VoiceLiveEventType
    payload: Dict[str, Any]


def map_sdk_event(event: Any) -> VoiceLiveEvent:
    """Translate a strongly typed SDK event into the gateway-friendly dataclass."""

    event_type_str = getattr(event, "type", "ERROR")
    
    # Map from SDK event type strings to enum names
    type_mapping = {
        "session.created": VoiceLiveEventType.SESSION_CREATED,
        "session.updated": VoiceLiveEventType.SESSION_UPDATED,
        "conversation.item.created": VoiceLiveEventType.CONVERSATION_ITEM_CREATED,
        "conversation.item.input_audio_transcription.delta": VoiceLiveEventType.INPUT_AUDIO_TRANSCRIPTION_DELTA,
        "conversation.item.input_audio_transcription.completed": VoiceLiveEventType.INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
        "input_audio_buffer.committed": VoiceLiveEventType.INPUT_AUDIO_BUFFER_COMMITTED,
        "input_audio_buffer.speech_started": VoiceLiveEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
        "input_audio_buffer.speech_stopped": VoiceLiveEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED,
        "response.created": VoiceLiveEventType.RESPONSE_CREATED,
        "response.output_item.added": VoiceLiveEventType.RESPONSE_OUTPUT_ITEM_ADDED,
        "response.output_item.done": VoiceLiveEventType.RESPONSE_OUTPUT_ITEM_DONE,
        "response.content_part.added": VoiceLiveEventType.RESPONSE_CONTENT_PART_ADDED,
        "response.content_part.done": VoiceLiveEventType.RESPONSE_CONTENT_PART_DONE,
        "response.audio.delta": VoiceLiveEventType.RESPONSE_AUDIO_DELTA,
        "response.audio.done": VoiceLiveEventType.RESPONSE_AUDIO_DONE,
        "response.audio_transcript.delta": VoiceLiveEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
        "response.audio_transcript.done": VoiceLiveEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE,
        "response.done": VoiceLiveEventType.RESPONSE_DONE,
    }
    
    event_type = type_mapping.get(event_type_str, VoiceLiveEventType.ERROR)
    payload = event.__dict__ if hasattr(event, "__dict__") else {"raw": event}
    return VoiceLiveEvent(type=event_type, payload=payload)
