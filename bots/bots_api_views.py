import logging

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import ApiKeyAuthentication
from .bots_api_utils import create_bot, create_bot_media_request_for_image, launch_bot, send_sync_command
from .models import (
    Bot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotMediaRequest,
    BotMediaRequestMediaTypes,
    BotMediaRequestStates,
    BotStates,
    Credentials,
    MediaBlob,
    MeetingTypes,
    Recording,
    Utterance,
)
from .serializers import (
    BotImageSerializer,
    BotSerializer,
    CreateBotSerializer,
    RecordingSerializer,
    SpeechSerializer,
    TranscriptUtteranceSerializer,
)
from .utils import meeting_type_from_url

TokenHeaderParameter = [
    OpenApiParameter(
        name="Authorization",
        type=str,
        location=OpenApiParameter.HEADER,
        description="API key for authentication",
        required=True,
        default="Token YOUR_API_KEY_HERE",
    ),
    OpenApiParameter(
        name="Content-Type",
        type=str,
        location=OpenApiParameter.HEADER,
        description="Should always be application/json",
        required=True,
        default="application/json",
    ),
]

LeavingBotExample = OpenApiExample(
    "Leaving Bot",
    value={
        "id": "bot_weIAju4OXNZkDTpZ",
        "meeting_url": "https://zoom.us/j/123?pwd=456",
        "state": "leaving",
        "events": [
            {"type": "join_requested", "created_at": "2024-01-18T12:34:56Z"},
            {"type": "joined_meeting", "created_at": "2024-01-18T12:35:00Z"},
            {"type": "leave_requested", "created_at": "2024-01-18T13:34:56Z"},
        ],
        "transcription_state": "in_progress",
        "recording_state": "in_progress",
    },
    description="Example response when requesting a bot to leave",
)

NewlyCreatedBotExample = OpenApiExample(
    "New bot",
    value={
        "id": "bot_weIAju4OXNZkDTpZ",
        "meeting_url": "https://zoom.us/j/123?pwd=456",
        "state": "joining",
        "events": [{"type": "join_requested", "created_at": "2024-01-18T12:34:56Z"}],
        "transcription_state": "not_started",
        "recording_state": "not_started",
    },
    description="Example response when creating a new bot",
)


@extend_schema(exclude=True)
class NotFoundView(APIView):
    def get(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def put(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def handle_request(self, request, *args, **kwargs):
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)


class BotCreateView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Create Bot",
        summary="Create a new bot",
        description="After being created, the bot will attempt to join the specified meeting.",
        request=CreateBotSerializer,
        responses={
            201: OpenApiResponse(
                response=BotSerializer,
                description="Bot created successfully",
                examples=[NewlyCreatedBotExample],
            ),
            400: OpenApiResponse(description="Invalid input"),
        },
        parameters=TokenHeaderParameter,
        tags=["Bots"],
    )
    def post(self, request):
        bot, error = create_bot(request.data, request.auth.project)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        launch_bot(bot)

        return Response(BotSerializer(bot).data, status=status.HTTP_201_CREATED)


class SpeechView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Output speech",
        summary="Output speech",
        description="Causes the bot to speak a message in the meeting.",
        request=SpeechSerializer,
        responses={
            200: OpenApiResponse(description="Speech request created successfully"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        # Get the bot
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)

        # Validate the request data
        serializer = SpeechSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Check if bot is in a state that can play media
        if not BotEventManager.is_state_that_can_play_media(bot.state):
            return Response(
                {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot play media"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check for Google TTS credentials. This is currently the only supported text-to-speech provider.
        google_tts_credentials = bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.GOOGLE_TTS).first()

        if not google_tts_credentials:
            settings_url = request.build_absolute_uri(reverse("bots:project-credentials", kwargs={"object_id": bot.project.object_id}))
            return Response(
                {"error": f"Google Text-to-Speech credentials are required. Please add credentials at {settings_url}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create the media request
        BotMediaRequest.objects.create(
            bot=bot,
            text_to_speak=serializer.validated_data["text"],
            text_to_speech_settings=serializer.validated_data["text_to_speech_settings"],
            media_type=BotMediaRequestMediaTypes.AUDIO,
        )

        # Send sync command to notify bot of new media request
        send_sync_command(bot, "sync_media_requests")

        return Response(status=status.HTTP_200_OK)


class OutputVideoView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Output video",
        summary="Output video",
        description="Causes the bot to output a video in the meeting.",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL of the video to output. Must be a valid URL to an mp4 file.",
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            }
        },
        responses={
            200: OpenApiResponse(description="Video request created successfully"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        # Get the bot
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)

        # Get which type of meeting the bot is in
        meeting_type = meeting_type_from_url(bot.meeting_url)
        if meeting_type != MeetingTypes.GOOGLE_MEET:
            # Video output is not supported in this meeting type
            return Response({"error": "Video output is not supported in this meeting type"}, status=status.HTTP_400_BAD_REQUEST)

        # Validate the request data
        url = request.data.get("url")
        if not url:
            return Response({"error": "URL is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not url.startswith("https://"):
            return Response({"error": "URL must start with https://"}, status=status.HTTP_400_BAD_REQUEST)
        if not url.endswith(".mp4"):
            return Response({"error": "URL must end with .mp4"}, status=status.HTTP_400_BAD_REQUEST)

        # Check if bot is in a state that can play media
        if not BotEventManager.is_state_that_can_play_media(bot.state):
            return Response(
                {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot play media"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if bot.media_requests.filter(state=BotMediaRequestStates.PLAYING).exists():
            return Response({"error": "Bot is already playing media. Please wait for it to finish."}, status=status.HTTP_400_BAD_REQUEST)

        # Create the media request
        BotMediaRequest.objects.create(
            bot=bot,
            media_type=BotMediaRequestMediaTypes.VIDEO,
            media_url=url,
        )

        # Send sync command to notify bot of new media request
        send_sync_command(bot, "sync_media_requests")

        return Response(status=status.HTTP_200_OK)


class OutputAudioView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Output Audio",
        summary="Output audio",
        description="Causes the bot to output audio in the meeting.",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [ct[0] for ct in MediaBlob.VALID_AUDIO_CONTENT_TYPES],
                    },
                    "data": {
                        "type": "string",
                        "description": "Base64 encoded audio data",
                    },
                },
                "required": ["type", "data"],
            }
        },
        responses={
            200: OpenApiResponse(description="Audio request created successfully"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            # Validate request data
            if "type" not in request.data or "data" not in request.data:
                return Response(
                    {"error": "Both type and data are required"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            content_type = request.data["type"]
            if content_type not in [ct[0] for ct in MediaBlob.VALID_AUDIO_CONTENT_TYPES]:
                return Response(
                    {"error": "Invalid audio content type"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                # Decode base64 data
                import base64

                audio_data = base64.b64decode(request.data["data"])
            except Exception:
                return Response(
                    {"error": "Invalid base64 encoded data"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Get the bot
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
            if not BotEventManager.is_state_that_can_play_media(bot.state):
                return Response(
                    {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot play media"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                # Create or get existing MediaBlob
                media_blob = MediaBlob.get_or_create_from_blob(project=request.auth.project, blob=audio_data, content_type=content_type)
            except Exception as e:
                error_message_first_line = str(e).split("\n")[0]
                logging.error(f"Error creating audio blob: {error_message_first_line} (content_type={content_type}, bot_id={object_id})")
                return Response({"error": f"Error creating the audio blob. Are you sure it's a valid {content_type} file?", "raw_error": error_message_first_line}, status=status.HTTP_400_BAD_REQUEST)

            # Create BotMediaRequest
            BotMediaRequest.objects.create(
                bot=bot,
                media_blob=media_blob,
                media_type=BotMediaRequestMediaTypes.AUDIO,
            )

            # Send sync command
            send_sync_command(bot, "sync_media_requests")

            return Response(status=status.HTTP_200_OK)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class OutputImageView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Output Image",
        summary="Output image",
        description="Causes the bot to output an image in the meeting.",
        request=BotImageSerializer,
        responses={
            200: OpenApiResponse(description="Image request created successfully"),
            400: OpenApiResponse(description="Invalid input"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            # Get the bot
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
            if not BotEventManager.is_state_that_can_play_media(bot.state):
                return Response(
                    {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot play media"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if bot.media_requests.filter(media_type=BotMediaRequestMediaTypes.VIDEO, state=BotMediaRequestStates.PLAYING).exists():
                return Response({"error": "Bot is already playing a video. Please wait for it to finish."}, status=status.HTTP_400_BAD_REQUEST)

            # Validate request data
            bot_image = BotImageSerializer(data=request.data)
            if not bot_image.is_valid():
                return Response(bot_image.errors, status=status.HTTP_400_BAD_REQUEST)

            try:
                create_bot_media_request_for_image(bot, bot_image.validated_data)
            except ValidationError as e:
                return Response({"error": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)

            # Send sync command
            send_sync_command(bot, "sync_media_requests")

            return Response(status=status.HTTP_200_OK)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class DeleteDataView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Delete Bot Data",
        summary="Delete bot data",
        description="Permanently deletes all data associated with this bot, including recordings, transcripts, and participant information. Metadata is not deleted. This cannot be undone.",
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Data successfully deleted",
            ),
            400: OpenApiResponse(description="Bot is not in a valid state for data deletion"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
            logging.info(f"Deleting data for bot {bot.object_id}")
            bot.delete_data()
            logging.info(f"Data deleted for bot {bot.object_id}")
            return Response(BotSerializer(bot).data, status=status.HTTP_200_OK)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logging.error(f"Error deleting bot data: {str(e)} (bot_id={object_id})")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )


class BotLeaveView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Leave Meeting",
        summary="Leave a meeting",
        description="Causes the bot to leave the meeting.",
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Successfully requested to leave meeting",
                examples=[LeavingBotExample],
            ),
            400: OpenApiResponse(description="Bot is not in a valid state to leave the meeting"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            BotEventManager.create_event(bot, BotEventTypes.LEAVE_REQUESTED, event_sub_type=BotEventSubTypes.LEAVE_REQUESTED_USER_REQUESTED)

            send_sync_command(bot)

            return Response(BotSerializer(bot).data, status=status.HTTP_200_OK)
        except ValidationError as e:
            logging.error(f"Error leaving meeting: {str(e)} (bot_id={object_id})")
            return Response({"error": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class RecordingView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Bot Recording",
        summary="Get the recording for a bot",
        description="Returns a short-lived S3 URL for the recording of the bot.",
        responses={
            200: OpenApiResponse(
                response=RecordingSerializer,
                description="Short-lived S3 URL for the recording",
            )
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            recording = Recording.objects.filter(bot=bot, is_default_recording=True).first()
            if not recording:
                return Response(
                    {"error": "No recording found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            recording_file = recording.file
            if not recording_file:
                return Response(
                    {"error": "No recording file found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            return Response(RecordingSerializer(recording).data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class TranscriptView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Bot Transcript",
        summary="Get the transcript for a bot",
        description="If the meeting is still in progress, this returns the transcript so far.",
        responses={
            200: OpenApiResponse(
                response=TranscriptUtteranceSerializer(many=True),
                description="List of transcribed utterances",
            ),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
            OpenApiParameter(
                name="updated_after",
                type={"type": "string", "format": "ISO 8601 datetime"},
                location=OpenApiParameter.QUERY,
                description="Only return transcript entries updated or created after this time. Useful when polling for updates to the transcript.",
                required=False,
                examples=[OpenApiExample("DateTime Example", value="2024-01-18T12:34:56Z")],
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            recording = Recording.objects.filter(bot=bot, is_default_recording=True).first()
            if not recording:
                return Response(
                    {"error": "No recording found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Get all utterances with transcriptions, sorted by timeline
            utterances_query = Utterance.objects.select_related("participant").filter(recording=recording, transcription__isnull=False)

            # Apply updated_after filter if provided
            updated_after = request.query_params.get("updated_after")
            if updated_after:
                try:
                    updated_after_datetime = parse_datetime(str(updated_after))
                except Exception:
                    updated_after_datetime = None

                if not updated_after_datetime:
                    return Response(
                        {"error": "Invalid updated_after format. Use ISO 8601 format (e.g., 2024-01-18T12:34:56Z)"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                utterances_query = utterances_query.filter(updated_at__gt=updated_after_datetime)

            # Apply ordering
            utterances = utterances_query.order_by("timestamp_ms")

            # Format the response, skipping empty transcriptions
            transcript_data = [
                {
                    "speaker_name": utterance.participant.full_name,
                    "speaker_uuid": utterance.participant.uuid,
                    "speaker_user_uuid": utterance.participant.user_uuid,
                    "timestamp_ms": utterance.timestamp_ms,
                    "duration_ms": utterance.duration_ms,
                    "transcription": utterance.transcription,
                }
                for utterance in utterances
                if utterance.transcription.get("transcript", "")
            ]

            serializer = TranscriptUtteranceSerializer(transcript_data, many=True)
            return Response(serializer.data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class BotDetailView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Bot",
        summary="Get the details for a bot",
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Bot details",
                examples=[NewlyCreatedBotExample],
            ),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
            return Response(BotSerializer(bot).data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)
