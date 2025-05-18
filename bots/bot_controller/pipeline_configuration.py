from dataclasses import dataclass
from typing import FrozenSet


# Specifies how the bot will use the media from the meeting platform
# For now there are only a few valid configurations, to avoid having to make the bot
# work for every possible configuration
@dataclass(frozen=True)
class PipelineConfiguration:
    record_video: bool
    record_audio: bool
    transcribe_audio: bool
    rtmp_stream_audio: bool
    rtmp_stream_video: bool

    def __post_init__(self):
        # Convert to FrozenSet of FrozenSet[str]
        valid_configurations: FrozenSet[FrozenSet[str]] = frozenset(
            {
                # Basic meeting bot configuration
                frozenset({"record_audio", "record_video", "transcribe_audio"}),
                # RTMP streaming configuration
                frozenset({"rtmp_stream_audio", "rtmp_stream_video", "transcribe_audio"}),
                # Voice agent configuration
                frozenset({"transcribe_audio"}),
            }
        )

        # Get the set of all fields that are True
        active_fields = frozenset(field for field in self.__dataclass_fields__.keys() if getattr(self, field))

        # Check if this combination exists in our valid configurations
        if active_fields not in valid_configurations:
            raise ValueError(f"Invalid configuration: {active_fields}\nMust be one of: {valid_configurations}")

    @classmethod
    def recorder_bot(cls) -> "PipelineConfiguration":
        return cls(
            record_video=True,
            record_audio=True,
            transcribe_audio=True,
            rtmp_stream_audio=False,
            rtmp_stream_video=False,
        )

    @classmethod
    def rtmp_streaming_bot(cls) -> "PipelineConfiguration":
        return cls(
            record_video=False,
            record_audio=False,
            transcribe_audio=True,
            rtmp_stream_audio=True,
            rtmp_stream_video=True,
        )

    @classmethod
    def voice_agent(cls) -> "PipelineConfiguration":
        return cls(
            record_video=False,
            record_audio=False,
            transcribe_audio=True,
            rtmp_stream_audio=False,
            rtmp_stream_video=False,
        )
