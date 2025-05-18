import base64
import json
from dataclasses import asdict

import jsonschema
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema_field,
    extend_schema_serializer,
)
from rest_framework import serializers

from .bot_controller.automatic_leave_configuration import AutomaticLeaveConfiguration
from .models import (
    Bot,
    BotEventSubTypes,
    BotEventTypes,
    BotStates,
    MediaBlob,
    MeetingTypes,
    Recording,
    RecordingFormats,
    RecordingStates,
    RecordingTranscriptionStates,
    RecordingViews,
    TranscriptionProviders,
)
from .utils import is_valid_png, meeting_type_from_url, transcription_provider_from_meeting_url_and_transcription_settings

# Define the schema once
BOT_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["image/png"]},
        "data": {
            "type": "string",
        },
    },
    "required": ["type", "data"],
    "additionalProperties": False,
}


@extend_schema_field(BOT_IMAGE_SCHEMA)
class ImageJSONField(serializers.JSONField):
    """Field for images with validation"""

    pass


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Valid image",
            value={
                "type": "image/png",
                "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
            },
            description="An image of a red pixel encoded in base64 in PNG format",
        )
    ]
)
class BotImageSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=[ct[0] for ct in MediaBlob.VALID_IMAGE_CONTENT_TYPES], help_text="Image content type. Currently only PNG is supported.")  # image/png
    data = serializers.CharField(help_text="Base64 encoded image data. Simple example of a red pixel encoded in PNG format: iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")  # base64 encoded image data

    def validate_type(self, value):
        """Validate the content type"""
        if value not in [ct[0] for ct in MediaBlob.VALID_IMAGE_CONTENT_TYPES]:
            raise serializers.ValidationError("Invalid image content type")
        return value

    def validate(self, data):
        """Validate the entire image data"""
        try:
            # Decode base64 data
            image_data = base64.b64decode(data.get("data", ""))
        except Exception:
            raise serializers.ValidationError("Invalid base64 encoded data")

        # Validate that it's a proper PNG image
        if not is_valid_png(image_data):
            raise serializers.ValidationError("Data is not a valid PNG image. This site can generate base64 encoded PNG images to test with: https://png-pixel.com")

        # Add the decoded data to the validated data
        data["decoded_data"] = image_data
        return data


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "deepgram": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "description": "The language code for transcription. Defaults to 'multi' if not specified, which selects the language automatically and can change the detected language in the middle of the audio. See here for available languages: https://developers.deepgram.com/docs/models-languages-overview.",
                    },
                    "detect_language": {
                        "type": "boolean",
                        "description": "Whether to automatically detect the spoken language. Can only detect a single language for the entire audio. This is only supported for an older model and is not recommended. Please use language='multi' instead.",
                    },
                    "callback": {
                        "type": "string",
                        "description": "The URL to send the transcriptions to. If used, the transcriptions will be sent directly from Deepgram to your server so you will not be able to access them via the Attendee API. See here for details: https://developers.deepgram.com/docs/callback",
                    },
                },
                "additionalProperties": False,
            },
            "gladia": {
                "type": "object",
                "properties": {
                    "code_switching_languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The languages to transcribe the meeting in when using code switching. See here for available languages: https://docs.gladia.io/chapters/limits-and-specifications/languages",
                    },
                    "enable_code_switching": {"type": "boolean", "description": "Whether to use code switching to transcribe the meeting in multiple languages."},
                },
                "additionalProperties": False,
            },
            "meeting_closed_captions": {
                "type": "object",
                "properties": {
                    "google_meet_language": {
                        "type": "string",
                        "description": "The language code for Google Meet closed captions (e.g. 'en-US'). See here for available languages and codes: https://docs.google.com/spreadsheets/d/1MN44lRrEBaosmVI9rtTzKMii86zGgDwEwg4LSj-SjiE",
                    },
                },
                "additionalProperties": False,
            },
            "openai": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "enum": ["gpt-4o-transcribe", "gpt-4o-mini-transcribe"],
                        "description": "The OpenAI model to use for transcription",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Optional prompt to use for the OpenAI transcription",
                    },
                },
                "required": ["model"],
                "additionalProperties": False,
            },
        },
        "required": [],
    }
)
class TranscriptionSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "destination_url": {
                "type": "string",
                "description": "The URL of the RTMP server to send the stream to",
            },
            "stream_key": {
                "type": "string",
                "description": "The stream key to use for the RTMP server",
            },
        },
        "required": ["destination_url", "stream_key"],
    }
)
class RTMPSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "description": "The format of the recording to save. The supported formats are 'mp4'.",
            },
            "view": {
                "type": "string",
                "description": "The view to use for the recording. The supported views are 'speaker_view' and 'gallery_view'.",
            },
        },
        "required": [],
    }
)
class RecordingSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "create_debug_recording": {
                "type": "boolean",
                "description": "Whether to generate a recording of the attempt to join the meeting. Used for debugging.",
            },
        },
        "required": [],
    }
)
class DebugSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_field({"type": "object", "description": "JSON object containing metadata to associate with the bot", "example": {"client_id": "abc123", "user": "john_doe", "purpose": "Weekly team meeting"}})
class MetadataJSONField(serializers.JSONField):
    pass


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "silence_timeout_seconds": {
                "type": "integer",
                "description": "Number of seconds of continuous silence after which the bot should leave",
                "default": 600,
            },
            "silence_activate_after_seconds": {
                "type": "integer",
                "description": "Number of seconds to wait before activating the silence timeout",
                "default": 1200,
            },
            "only_participant_in_meeting_timeout_seconds": {
                "type": "integer",
                "description": "Number of seconds to wait before leaving if bot is the only participant",
                "default": 60,
            },
            "wait_for_host_to_start_meeting_timeout_seconds": {
                "type": "integer",
                "description": "Number of seconds to wait for the host to start the meeting",
                "default": 600,
            },
            "waiting_room_timeout_seconds": {
                "type": "integer",
                "description": "Number of seconds to wait before leaving if the bot is in the waiting room",
                "default": 900,
            },
        },
        "required": [],
        "additionalProperties": False,
    }
)
class AutomaticLeaveSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Valid meeting URL",
            value={
                "meeting_url": "https://zoom.us/j/123?pwd=456",
                "bot_name": "My Bot",
            },
            description="Example of a valid Zoom meeting URL",
        )
    ]
)
class CreateBotSerializer(serializers.Serializer):
    meeting_url = serializers.CharField(help_text="The URL of the meeting to join, e.g. https://zoom.us/j/123?pwd=456")
    bot_name = serializers.CharField(help_text="The name of the bot to create, e.g. 'My Bot'")
    bot_image = BotImageSerializer(help_text="The image for the bot", required=False, default=None)
    metadata = MetadataJSONField(help_text="JSON object containing metadata to associate with the bot", required=False, default=None)

    transcription_settings = TranscriptionSettingsJSONField(
        help_text="The transcription settings for the bot, e.g. {'deepgram': {'language': 'en'}}",
        required=False,
        default=None,
    )

    TRANSCRIPTION_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "deepgram": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                    },
                    "detect_language": {"type": "boolean"},
                    "callback": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "gladia": {
                "type": "object",
                "properties": {
                    "code_switching_languages": {"type": "array", "items": {"type": "string"}},
                    "enable_code_switching": {"type": "boolean"},
                },
                "required": [],
                "additionalProperties": False,
            },
            "openai": {
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "enum": ["gpt-4o-transcribe", "gpt-4o-mini-transcribe"],
                        "description": "The OpenAI model to use for transcription",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Optional prompt to use for the OpenAI transcription",
                    },
                },
                "required": ["model"],
                "additionalProperties": False,
            },
            "meeting_closed_captions": {
                "type": "object",
                "properties": {
                    "google_meet_language": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    def validate_meeting_url(self, value):
        meeting_type = meeting_type_from_url(value)
        if meeting_type is None:
            raise serializers.ValidationError("Invalid meeting URL")

        if meeting_type == MeetingTypes.GOOGLE_MEET:
            if not value.startswith("https://meet.google.com/"):
                raise serializers.ValidationError("Google Meet URL must start with https://meet.google.com/")

        return value

    def validate_transcription_settings(self, value):
        meeting_url = self.initial_data.get("meeting_url")
        meeting_type = meeting_type_from_url(meeting_url)

        if value is None:
            if meeting_type == MeetingTypes.ZOOM:
                value = {"deepgram": {"language": "multi"}}
            elif meeting_type == MeetingTypes.GOOGLE_MEET:
                value = {"meeting_closed_captions": {}}
            elif meeting_type == MeetingTypes.TEAMS:
                value = {"meeting_closed_captions": {}}
            else:
                return None

        try:
            jsonschema.validate(instance=value, schema=self.TRANSCRIPTION_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # If deepgram key is specified but language is not, set to "multi"
        if "deepgram" in value and ("language" not in value["deepgram"] or value["deepgram"]["language"] is None):
            value["deepgram"]["language"] = "multi"

        if meeting_type == MeetingTypes.TEAMS:
            if transcription_provider_from_meeting_url_and_transcription_settings(meeting_url, value) != TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM:
                raise serializers.ValidationError({"transcription_settings": "API-based transcription is not supported for Teams. Please use Meeting Closed Captions to transcribe Teams meetings."})

        if meeting_type == MeetingTypes.ZOOM:
            if transcription_provider_from_meeting_url_and_transcription_settings(meeting_url, value) == TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM:
                raise serializers.ValidationError({"transcription_settings": "Closed caption based transcription is not supported for Zoom. Please use Deepgram to transcribe Zoom meetings."})

        if value.get("deepgram", {}).get("callback") and value.get("deepgram", {}).get("detect_language"):
            raise serializers.ValidationError({"transcription_settings": "Language detection is not supported for streaming transcription. Please pass language='multi' instead of detect_language=true."})

        return value

    rtmp_settings = RTMPSettingsJSONField(
        help_text="RTMP server to stream to, e.g. {'destination_url': 'rtmp://global-live.mux.com:5222/app', 'stream_key': 'xxxx'}.",
        required=False,
        default=None,
    )

    RTMP_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "destination_url": {"type": "string"},
            "stream_key": {"type": "string"},
        },
        "required": ["destination_url", "stream_key"],
    }

    def validate_rtmp_settings(self, value):
        if value is None:
            return value

        try:
            jsonschema.validate(instance=value, schema=self.RTMP_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # Validate RTMP URL format
        destination_url = value.get("destination_url", "")
        if not (destination_url.lower().startswith("rtmp://") or destination_url.lower().startswith("rtmps://")):
            raise serializers.ValidationError({"destination_url": "URL must start with rtmp:// or rtmps://"})

        return value

    recording_settings = RecordingSettingsJSONField(
        help_text="The settings for the bot's recording. Currently the only setting is 'view' which can be 'speaker_view' or 'gallery_view'.",
        required=False,
        default={"format": RecordingFormats.MP4, "view": RecordingViews.SPEAKER_VIEW},
    )

    RECORDING_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "format": {"type": "string"},
            "view": {"type": "string"},
        },
        "required": [],
    }

    def validate_recording_settings(self, value):
        if value is None:
            return value

        try:
            jsonschema.validate(instance=value, schema=self.RECORDING_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        # Validate format if provided
        format = value.get("format")
        if format not in [RecordingFormats.MP4, None]:
            raise serializers.ValidationError({"format": "Format must be mp4"})

        # Validate view if provided
        view = value.get("view")
        if view not in [RecordingViews.SPEAKER_VIEW, RecordingViews.GALLERY_VIEW, None]:
            raise serializers.ValidationError({"view": "View must be speaker_view or gallery_view"})

        return value

    debug_settings = DebugSettingsJSONField(
        help_text="The debug settings for the bot, e.g. {'create_debug_recording': True}.",
        required=False,
        default={"create_debug_recording": False},
    )

    DEBUG_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "create_debug_recording": {"type": "boolean"},
        },
        "required": [],
        "additionalProperties": False,
    }

    def validate_debug_settings(self, value):
        if value is None:
            return value

        try:
            jsonschema.validate(instance=value, schema=self.DEBUG_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        return value

    def validate_metadata(self, value):
        if value is None:
            return value

        # Check if it's a dict
        if not isinstance(value, dict):
            raise serializers.ValidationError("Metadata must be an object not an array or other type")

        # Make sure there is at least one key
        if not value:
            raise serializers.ValidationError("Metadata must have at least one key")

        # Check if all values are strings
        for key, val in value.items():
            if not isinstance(val, str):
                raise serializers.ValidationError(f"Value for key '{key}' must be a string")

        # Check if all keys are strings
        for key in value.keys():
            if not isinstance(key, str):
                raise serializers.ValidationError("All keys in metadata must be strings")

        # Make sure the total length of the stringified metadata is less than 1000 characters
        if len(json.dumps(value)) > 1000:
            raise serializers.ValidationError("Metadata must be less than 1000 characters")

        return value

    automatic_leave_settings = AutomaticLeaveSettingsJSONField(default=dict, required=False)

    def validate_automatic_leave_settings(self, value):
        # Set default values if not provided
        defaults = asdict(AutomaticLeaveConfiguration())

        # Validate that an unexpected key is not provided
        for key in value.keys():
            if key not in defaults.keys():
                raise serializers.ValidationError(f"Unexpected attribute: {key}")

        # Validate that all values are positive integers
        for param, default in defaults.items():
            if param in value and (not isinstance(value[param], int) or value[param] <= 0):
                raise serializers.ValidationError(f"{param} must be a positive integer")
            # Set default if not provided
            if param not in value:
                value[param] = default

        return value

    def validate_bot_name(self, value):
        """Validate that the bot name only contains characters in the Basic Multilingual Plane (BMP)."""
        for char in value:
            if ord(char) > 0xFFFF:
                raise serializers.ValidationError("Bot name cannot contain emojis or rare script characters.")
        return value


class BotSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="object_id")
    metadata = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    events = serializers.SerializerMethodField()
    transcription_state = serializers.SerializerMethodField()
    recording_state = serializers.SerializerMethodField()

    @extend_schema_field(
        {
            "type": "string",
            "enum": [BotStates.state_to_api_code(state.value) for state in BotStates],
        }
    )
    def get_state(self, obj):
        return BotStates.state_to_api_code(obj.state)

    @extend_schema_field({"type": "object", "description": "Metadata associated with the bot"})
    def get_metadata(self, obj):
        return obj.metadata

    @extend_schema_field(
        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "sub_type": {"type": "string", "nullable": True},
                    "created_at": {"type": "string", "format": "date-time"},
                },
            },
        }
    )
    def get_events(self, obj):
        events = []
        for event in obj.bot_events.all():
            event_type = BotEventTypes.type_to_api_code(event.event_type)
            event_data = {"type": event_type, "created_at": event.created_at}

            if event.event_sub_type:
                event_data["sub_type"] = BotEventSubTypes.sub_type_to_api_code(event.event_sub_type)

            events.append(event_data)
        return events

    @extend_schema_field(
        {
            "type": "string",
            "enum": [RecordingTranscriptionStates.state_to_api_code(state.value) for state in RecordingTranscriptionStates],
        }
    )
    def get_transcription_state(self, obj):
        default_recording = Recording.objects.filter(bot=obj, is_default_recording=True).first()
        if not default_recording:
            return None

        return RecordingTranscriptionStates.state_to_api_code(default_recording.transcription_state)

    @extend_schema_field(
        {
            "type": "string",
            "enum": [RecordingStates.state_to_api_code(state.value) for state in RecordingStates],
        }
    )
    def get_recording_state(self, obj):
        default_recording = Recording.objects.filter(bot=obj, is_default_recording=True).first()
        if not default_recording:
            return None

        return RecordingStates.state_to_api_code(default_recording.state)

    class Meta:
        model = Bot
        fields = [
            "id",
            "metadata",
            "meeting_url",
            "state",
            "events",
            "transcription_state",
            "recording_state",
        ]
        read_only_fields = fields


class TranscriptUtteranceSerializer(serializers.Serializer):
    speaker_name = serializers.CharField()
    speaker_uuid = serializers.CharField()
    speaker_user_uuid = serializers.CharField(allow_null=True)
    timestamp_ms = serializers.IntegerField()
    duration_ms = serializers.IntegerField()
    transcription = serializers.JSONField()


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Recording Upload",
            value={
                "url": "https://attendee-short-term-storage-production.s3.amazonaws.com/e4da3b7fbbce2345d7772b0674a318d5.mp4?...",
                "start_timestamp_ms": 1733114771000,
            },
        )
    ]
)
class RecordingSerializer(serializers.ModelSerializer):
    start_timestamp_ms = serializers.IntegerField(source="first_buffer_timestamp_ms")

    class Meta:
        model = Recording
        fields = ["url", "start_timestamp_ms"]


@extend_schema_field(
    {
        "type": "object",
        "properties": {
            "google": {
                "type": "object",
                "properties": {
                    "voice_language_code": {
                        "type": "string",
                        "description": "The voice language code (e.g. 'en-US'). See https://cloud.google.com/text-to-speech/docs/voices for a list of available language codes and voices.",
                    },
                    "voice_name": {
                        "type": "string",
                        "description": "The name of the voice to use (e.g. 'en-US-Casual-K')",
                    },
                },
            }
        },
        "required": ["google"],
    }
)
class TextToSpeechSettingsJSONField(serializers.JSONField):
    pass


@extend_schema_serializer(
    examples=[
        OpenApiExample(
            "Valid speech request",
            value={
                "text": "Hello, this is a bot speaking text.",
                "text_to_speech_settings": {
                    "google": {
                        "voice_language_code": "en-US",
                        "voice_name": "en-US-Casual-K",
                    }
                },
            },
            description="Example of a valid speech request",
        )
    ]
)
class SpeechSerializer(serializers.Serializer):
    text = serializers.CharField()
    text_to_speech_settings = TextToSpeechSettingsJSONField()

    TEXT_TO_SPEECH_SETTINGS_SCHEMA = {
        "type": "object",
        "properties": {
            "google": {
                "type": "object",
                "properties": {
                    "voice_language_code": {"type": "string"},
                    "voice_name": {"type": "string"},
                },
                "required": ["voice_language_code", "voice_name"],
                "additionalProperties": False,
            }
        },
        "required": ["google"],
        "additionalProperties": False,
    }

    def validate_text_to_speech_settings(self, value):
        if value is None:
            return None

        try:
            jsonschema.validate(instance=value, schema=self.TEXT_TO_SPEECH_SETTINGS_SCHEMA)
        except jsonschema.exceptions.ValidationError as e:
            raise serializers.ValidationError(e.message)

        return value
