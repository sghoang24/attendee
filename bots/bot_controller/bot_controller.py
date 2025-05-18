import json
import logging
import os
import signal
import threading
import time
import traceback
from datetime import timedelta

import gi
import redis
from django.core.files.base import ContentFile
from django.utils import timezone

from bots.bot_adapter import BotAdapter
from bots.models import (
    Bot,
    BotDebugScreenshot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotMediaRequestManager,
    BotMediaRequestMediaTypes,
    BotMediaRequestStates,
    BotStates,
    Credentials,
    MeetingTypes,
    Participant,
    Recording,
    RecordingFormats,
    RecordingManager,
    RecordingStates,
    TranscriptionProviders,
    Utterance,
)
from bots.utils import meeting_type_from_url, video_summarizer

from .audio_output_manager import AudioOutputManager
from .automatic_leave_configuration import AutomaticLeaveConfiguration
from .closed_caption_manager import ClosedCaptionManager
from .file_uploader import FileUploader
from .gstreamer_pipeline import GstreamerPipeline
from .per_participant_non_streaming_audio_input_manager import PerParticipantNonStreamingAudioInputManager
from .per_participant_streaming_audio_input_manager import PerParticipantStreamingAudioInputManager
from .pipeline_configuration import PipelineConfiguration
from .rtmp_client import RTMPClient
from .screen_and_audio_recorder import ScreenAndAudioRecorder
from .video_output_manager import VideoOutputManager

gi.require_version("GLib", "2.0")
from gi.repository import GLib

logger = logging.getLogger(__name__)


class BotController:
    def per_participant_audio_input_manager(self):
        if self.bot_in_db.deepgram_use_streaming():
            return self.per_participant_streaming_audio_input_manager
        else:
            return self.per_participant_non_streaming_audio_input_manager

    def get_google_meet_bot_adapter(self):
        from bots.google_meet_bot_adapter import GoogleMeetBotAdapter

        if self.get_recording_transcription_provider() == TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM:
            add_audio_chunk_callback = None
        else:
            add_audio_chunk_callback = self.per_participant_audio_input_manager().add_chunk

        return GoogleMeetBotAdapter(
            display_name=self.bot_in_db.name,
            send_message_callback=self.on_message_from_adapter,
            add_audio_chunk_callback=add_audio_chunk_callback,
            meeting_url=self.bot_in_db.meeting_url,
            add_video_frame_callback=None,
            wants_any_video_frames_callback=None,
            add_mixed_audio_chunk_callback=None,
            upsert_caption_callback=self.closed_caption_manager.upsert_caption,
            automatic_leave_configuration=self.automatic_leave_configuration,
            add_encoded_mp4_chunk_callback=None,
            recording_view=self.bot_in_db.recording_view(),
            google_meet_closed_captions_language=self.bot_in_db.google_meet_closed_captions_language(),
            should_create_debug_recording=self.bot_in_db.create_debug_recording(),
            start_recording_screen_callback=self.screen_and_audio_recorder.start_recording,
            stop_recording_screen_callback=self.screen_and_audio_recorder.stop_recording,
        )

    def get_teams_bot_adapter(self):
        from bots.teams_bot_adapter import TeamsBotAdapter

        return TeamsBotAdapter(
            display_name=self.bot_in_db.name,
            send_message_callback=self.on_message_from_adapter,
            add_audio_chunk_callback=None,
            meeting_url=self.bot_in_db.meeting_url,
            add_video_frame_callback=None,
            wants_any_video_frames_callback=None,
            add_mixed_audio_chunk_callback=None,
            upsert_caption_callback=self.closed_caption_manager.upsert_caption,
            automatic_leave_configuration=self.automatic_leave_configuration,
            add_encoded_mp4_chunk_callback=None,
            recording_view=self.bot_in_db.recording_view(),
            should_create_debug_recording=self.bot_in_db.create_debug_recording(),
            start_recording_screen_callback=self.screen_and_audio_recorder.start_recording,
            stop_recording_screen_callback=self.screen_and_audio_recorder.stop_recording,
        )

    def get_zoom_bot_adapter(self):
        from bots.zoom_bot_adapter import ZoomBotAdapter

        zoom_oauth_credentials_record = self.bot_in_db.project.credentials.filter(credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).first()
        if not zoom_oauth_credentials_record:
            raise Exception("Zoom OAuth credentials not found")

        zoom_oauth_credentials = zoom_oauth_credentials_record.get_credentials()
        if not zoom_oauth_credentials:
            raise Exception("Zoom OAuth credentials data not found")

        add_audio_chunk_callback = self.per_participant_audio_input_manager().add_chunk

        return ZoomBotAdapter(
            use_one_way_audio=self.pipeline_configuration.transcribe_audio,
            use_mixed_audio=self.pipeline_configuration.record_audio or self.pipeline_configuration.rtmp_stream_audio,
            use_video=self.pipeline_configuration.record_video or self.pipeline_configuration.rtmp_stream_video,
            display_name=self.bot_in_db.name,
            send_message_callback=self.on_message_from_adapter,
            add_audio_chunk_callback=add_audio_chunk_callback,
            zoom_client_id=zoom_oauth_credentials["client_id"],
            zoom_client_secret=zoom_oauth_credentials["client_secret"],
            meeting_url=self.bot_in_db.meeting_url,
            add_video_frame_callback=self.gstreamer_pipeline.on_new_video_frame,
            wants_any_video_frames_callback=self.gstreamer_pipeline.wants_any_video_frames,
            add_mixed_audio_chunk_callback=self.gstreamer_pipeline.on_mixed_audio_raw_data_received_callback,
            automatic_leave_configuration=self.automatic_leave_configuration,
        )

    def get_meeting_type(self):
        meeting_type = meeting_type_from_url(self.bot_in_db.meeting_url)
        if meeting_type is None:
            raise Exception(f"Could not determine meeting type for meeting url {self.bot_in_db.meeting_url}")
        return meeting_type

    def get_per_participant_audio_sample_rate(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            return 32000
        elif meeting_type == MeetingTypes.GOOGLE_MEET:
            return 48000
        elif meeting_type == MeetingTypes.TEAMS:
            return 48000

    def get_audio_format(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            return GstreamerPipeline.AUDIO_FORMAT_PCM
        elif meeting_type == MeetingTypes.GOOGLE_MEET:
            return GstreamerPipeline.AUDIO_FORMAT_FLOAT
        elif meeting_type == MeetingTypes.TEAMS:
            return GstreamerPipeline.AUDIO_FORMAT_FLOAT

    def get_sleep_time_between_audio_output_chunks_seconds(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            return 0.9
        return 0.1

    def get_num_audio_sources(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            return 1
        elif meeting_type == MeetingTypes.GOOGLE_MEET:
            return 3
        elif meeting_type == MeetingTypes.TEAMS:
            return 1

    def get_bot_adapter(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            return self.get_zoom_bot_adapter()
        elif meeting_type == MeetingTypes.GOOGLE_MEET:
            return self.get_google_meet_bot_adapter()
        elif meeting_type == MeetingTypes.TEAMS:
            return self.get_teams_bot_adapter()

    def get_first_buffer_timestamp_ms(self):
        if self.screen_and_audio_recorder:
            return self.adapter.get_first_buffer_timestamp_ms()

        if self.gstreamer_pipeline:
            if self.gstreamer_pipeline.start_time_ns is None:
                return None
            return int(self.gstreamer_pipeline.start_time_ns / 1_000_000) + self.adapter.get_first_buffer_timestamp_ms_offset()

    def recording_file_saved(self, s3_storage_key):
        recording = Recording.objects.get(bot=self.bot_in_db, is_default_recording=True)
        recording.file = s3_storage_key
        recording.first_buffer_timestamp_ms = self.get_first_buffer_timestamp_ms()
        recording.save()

    def get_recording_transcription_provider(self):
        recording = Recording.objects.get(bot=self.bot_in_db, is_default_recording=True)
        return recording.transcription_provider

    def get_recording_filename(self):
        recording = Recording.objects.get(bot=self.bot_in_db, is_default_recording=True)
        return f"{recording.object_id}.{self.bot_in_db.recording_format()}"

    def on_rtmp_connection_failed(self):
        logger.info("RTMP connection failed")
        BotEventManager.create_event(
            bot=self.bot_in_db,
            event_type=BotEventTypes.FATAL_ERROR,
            event_sub_type=BotEventSubTypes.FATAL_ERROR_RTMP_CONNECTION_FAILED,
            event_metadata={"rtmp_destination_url": self.bot_in_db.rtmp_destination_url()},
        )
        self.cleanup()

    def on_new_sample_from_gstreamer_pipeline(self, data):
        # For now, we'll assume that if rtmp streaming is enabled, we don't need to upload to s3
        if self.rtmp_client:
            write_succeeded = self.rtmp_client.write_data(data)
            if not write_succeeded:
                GLib.idle_add(lambda: self.on_rtmp_connection_failed())
        else:
            raise Exception("No rtmp client found")

    def cleanup(self):
        if self.cleanup_called:
            logger.info("Cleanup already called, exiting")
            return
        self.cleanup_called = True

        normal_quitting_process_worked = False
        import threading

        def terminate_worker():
            import time

            time.sleep(600)
            if normal_quitting_process_worked:
                logger.info("Normal quitting process worked, not force terminating worker")
                return
            logger.info("Terminating worker with hard timeout...")
            os.kill(os.getpid(), signal.SIGKILL)  # Force terminate the worker process

        termination_thread = threading.Thread(target=terminate_worker, daemon=True)
        termination_thread.start()

        if self.gstreamer_pipeline:
            logger.info("Telling gstreamer pipeline to cleanup...")
            self.gstreamer_pipeline.cleanup()

        if self.rtmp_client:
            logger.info("Telling rtmp client to cleanup...")
            self.rtmp_client.stop()

        if self.adapter:
            logger.info("Telling adapter to leave meeting...")
            self.adapter.leave()
            logger.info("Telling adapter to cleanup...")
            self.adapter.cleanup()

        if self.main_loop and self.main_loop.is_running():
            self.main_loop.quit()

        if self.screen_and_audio_recorder:
            logger.info("Telling media recorder receiver to cleanup...")
            self.screen_and_audio_recorder.cleanup()

        if self.get_recording_file_location():
            # logger.info("Telling file uploader to upload recording file...")
            # file_uploader = FileUploader(
            #     os.environ.get("AWS_RECORDING_STORAGE_BUCKET_NAME"),
            #     self.get_recording_filename(),
            # )
            # file_uploader.upload_file(self.get_recording_file_location())
            # file_uploader.wait_for_upload()
            # logger.info("File uploader finished uploading file")

            # Summarize video
            logger.info("Summarizing video...")
            summarize_content = video_summarizer(
                file_path=self.get_recording_file_location()
            )
            logger.info(f"Video summarization result: {summarize_content}")

            # Write summarized content to a .md file
            recording_filename = os.path.splitext(self.get_recording_filename())[0]
            summary_file_path = os.path.join("/tmp", f"{recording_filename}.md")
            with open(summary_file_path, "w", encoding='utf-8') as summary_file:
                summary_file.write(summarize_content)
            logger.info(f"[+] Summarized content written to {summary_file_path}")

            # Upload the .md file to S3
            summary_file_uploader = FileUploader(
                os.environ.get("AWS_RECORDING_STORAGE_BUCKET_NAME"),
                os.path.basename(summary_file_path),
            )
            summary_file_uploader.upload_file(summary_file_path)
            summary_file_uploader.wait_for_upload()
            logger.info("Summary file uploader finished uploading MD file")

            summary_file_uploader.delete_file(summary_file_path)
            logger.info("File uploader deleted file from local filesystem")
            self.recording_file_saved(summary_file_uploader.key)

        if self.bot_in_db.create_debug_recording():
            self.save_debug_recording()

        if self.bot_in_db.state == BotStates.POST_PROCESSING:
            BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.POST_PROCESSING_COMPLETED)

        normal_quitting_process_worked = True

    def __init__(self, bot_id):
        self.bot_in_db = Bot.objects.get(id=bot_id)
        self.cleanup_called = False
        self.run_called = False

        self.redis_client = None
        self.pubsub = None
        self.pubsub_channel = f"bot_{self.bot_in_db.id}"

        self.automatic_leave_configuration = AutomaticLeaveConfiguration(**self.bot_in_db.automatic_leave_settings())

        if self.bot_in_db.rtmp_destination_url():
            self.pipeline_configuration = PipelineConfiguration.rtmp_streaming_bot()
        else:
            self.pipeline_configuration = PipelineConfiguration.recorder_bot()

    def get_gstreamer_sink_type(self):
        if self.pipeline_configuration.rtmp_stream_audio or self.pipeline_configuration.rtmp_stream_video:
            return GstreamerPipeline.SINK_TYPE_APPSINK
        else:
            return GstreamerPipeline.SINK_TYPE_FILE

    def get_gstreamer_output_format(self):
        if self.pipeline_configuration.rtmp_stream_audio or self.pipeline_configuration.rtmp_stream_video:
            return GstreamerPipeline.OUTPUT_FORMAT_FLV

        if self.bot_in_db.recording_format() == RecordingFormats.WEBM:
            return GstreamerPipeline.OUTPUT_FORMAT_WEBM
        else:
            return GstreamerPipeline.OUTPUT_FORMAT_MP4

    def get_recording_file_location(self):
        if self.pipeline_configuration.rtmp_stream_audio or self.pipeline_configuration.rtmp_stream_video:
            return None
        else:
            return os.path.join("/tmp", self.get_recording_filename())

    def should_create_gstreamer_pipeline(self):
        # For google meet / teams, we're doing a media recorder based recording technique that does the video processing in the browser
        # so we don't need to create a gstreamer pipeline here
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            return True
        elif meeting_type == MeetingTypes.GOOGLE_MEET:
            return False
        elif meeting_type == MeetingTypes.TEAMS:
            return False

    def should_create_screen_and_audio_recorder(self):
        return not self.should_create_gstreamer_pipeline()

    def connect_to_redis(self):
        # Close both pubsub and client if they exist
        if self.pubsub:
            self.pubsub.close()
        if self.redis_client:
            self.redis_client.close()

        redis_url = os.getenv("REDIS_URL") + ("?ssl_cert_reqs=none" if os.getenv("DISABLE_REDIS_SSL") else "")
        self.redis_client = redis.from_url(redis_url)
        self.pubsub = self.redis_client.pubsub()
        self.pubsub.subscribe(self.pubsub_channel)
        logger.info(f"Redis connection established for bot {self.bot_in_db.id}")

    def run(self):
        if self.run_called:
            raise Exception("Run already called, exiting")
        self.run_called = True

        self.connect_to_redis()

        # Initialize core objects
        # Only used for adapters that can provide per-participant audio
        self.per_participant_non_streaming_audio_input_manager = PerParticipantNonStreamingAudioInputManager(
            save_utterance_callback=self.save_individual_audio_utterance,
            get_participant_callback=self.get_participant,
            sample_rate=self.get_per_participant_audio_sample_rate(),
        )

        self.per_participant_streaming_audio_input_manager = PerParticipantStreamingAudioInputManager(
            save_utterance_callback=self.save_individual_audio_utterance,
            get_participant_callback=self.get_participant,
            sample_rate=self.get_per_participant_audio_sample_rate(),
            transcription_provider=self.get_recording_transcription_provider(),
            bot=self.bot_in_db,
        )

        # Only used for adapters that can provide closed captions
        self.closed_caption_manager = ClosedCaptionManager(
            save_utterance_callback=self.save_closed_caption_utterance,
            get_participant_callback=self.get_participant,
        )

        self.rtmp_client = None
        if self.pipeline_configuration.rtmp_stream_audio or self.pipeline_configuration.rtmp_stream_video:
            self.rtmp_client = RTMPClient(rtmp_url=self.bot_in_db.rtmp_destination_url())
            self.rtmp_client.start()

        self.gstreamer_pipeline = None
        if self.should_create_gstreamer_pipeline():
            self.gstreamer_pipeline = GstreamerPipeline(
                on_new_sample_callback=self.on_new_sample_from_gstreamer_pipeline,
                video_frame_size=(1920, 1080),
                audio_format=self.get_audio_format(),
                output_format=self.get_gstreamer_output_format(),
                num_audio_sources=self.get_num_audio_sources(),
                sink_type=self.get_gstreamer_sink_type(),
                file_location=self.get_recording_file_location(),
            )
            self.gstreamer_pipeline.setup()

        self.screen_and_audio_recorder = None
        if self.should_create_screen_and_audio_recorder():
            self.screen_and_audio_recorder = ScreenAndAudioRecorder(
                file_location=self.get_recording_file_location(),
            )

        self.adapter = self.get_bot_adapter()

        self.audio_output_manager = AudioOutputManager(
            currently_playing_audio_media_request_finished_callback=self.currently_playing_audio_media_request_finished,
            play_raw_audio_callback=self.adapter.send_raw_audio,
            sleep_time_between_chunks_seconds=self.get_sleep_time_between_audio_output_chunks_seconds(),
        )

        self.video_output_manager = VideoOutputManager(
            currently_playing_video_media_request_finished_callback=self.currently_playing_video_media_request_finished,
            check_if_currently_playing_video_media_request_is_still_playing_callback=self.adapter.is_sent_video_still_playing,
            play_video_callback=self.adapter.send_video,
        )

        # Create GLib main loop
        self.main_loop = GLib.MainLoop()

        def repeatedly_try_to_reconnect_to_redis():
            reconnect_delay_seconds = 1
            num_attempts = 0
            while True:
                try:
                    self.connect_to_redis()
                    break
                except Exception as e:
                    logger.info(f"Error reconnecting to Redis: {e} Attempt {num_attempts} / 30.")
                    time.sleep(reconnect_delay_seconds)
                    num_attempts += 1
                    if num_attempts > 30:
                        raise Exception("Failed to reconnect to Redis after 30 attempts")

        def redis_listener():
            while True:
                try:
                    message = self.pubsub.get_message(timeout=1.0)
                    if message:
                        # Schedule Redis message handling in the main GLib loop
                        GLib.idle_add(lambda: self.handle_redis_message(message))
                except Exception as e:
                    # If this is a certain type of exception, we can attempt to reconnect
                    if isinstance(e, redis.exceptions.ConnectionError) and "Connection closed by server." in str(e):
                        logger.info("Redis connection closed by server. Attempting to reconnect...")
                        repeatedly_try_to_reconnect_to_redis()

                    else:
                        # log the type of exception
                        logger.info(f"Error in Redis listener: {type(e)} {e}")
                        break

        redis_thread = threading.Thread(target=redis_listener, daemon=True)
        redis_thread.start()

        # Add timeout just for audio processing
        self.first_timeout_call = True
        GLib.timeout_add(100, self.on_main_loop_timeout)

        # Add signal handlers so that when we get a SIGTERM or SIGINT, we can clean up the bot
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self.handle_glib_shutdown)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self.handle_glib_shutdown)

        # Run the main loop
        try:
            self.main_loop.run()
        except Exception as e:
            logger.info(f"Error in bot {self.bot_in_db.id}: {str(e)}")
            self.cleanup()
        finally:
            # Clean up Redis subscription
            self.pubsub.unsubscribe(self.pubsub_channel)
            self.pubsub.close()

    def take_action_based_on_bot_in_db(self):
        if self.bot_in_db.state == BotStates.JOINING:
            logger.info("take_action_based_on_bot_in_db - JOINING")
            BotEventManager.set_requested_bot_action_taken_at(self.bot_in_db)
            self.adapter.init()
        if self.bot_in_db.state == BotStates.LEAVING:
            logger.info("take_action_based_on_bot_in_db - LEAVING")
            BotEventManager.set_requested_bot_action_taken_at(self.bot_in_db)
            self.adapter.leave()

    def get_participant(self, participant_id):
        return self.adapter.get_participant(participant_id)

    def currently_playing_audio_media_request_finished(self, audio_media_request):
        logger.info("currently_playing_audio_media_request_finished called")
        BotMediaRequestManager.set_media_request_finished(audio_media_request)
        self.take_action_based_on_audio_media_requests_in_db()

    def currently_playing_video_media_request_finished(self, video_media_request):
        logger.info("currently_playing_video_media_request_finished called")
        BotMediaRequestManager.set_media_request_finished(video_media_request)
        self.take_action_based_on_video_media_requests_in_db()

    def take_action_based_on_audio_media_requests_in_db(self):
        media_type = BotMediaRequestMediaTypes.AUDIO
        oldest_enqueued_media_request = self.bot_in_db.media_requests.filter(state=BotMediaRequestStates.ENQUEUED, media_type=media_type).order_by("created_at").first()
        if not oldest_enqueued_media_request:
            return
        currently_playing_media_request = self.bot_in_db.media_requests.filter(state=BotMediaRequestStates.PLAYING, media_type=media_type).first()
        if currently_playing_media_request:
            logger.info(f"Currently playing media request {currently_playing_media_request.id} so cannot play another media request")
            return

        try:
            BotMediaRequestManager.set_media_request_playing(oldest_enqueued_media_request)
            self.audio_output_manager.start_playing_audio_media_request(oldest_enqueued_media_request)
        except Exception as e:
            logger.info(f"Error sending raw audio: {e}")
            BotMediaRequestManager.set_media_request_failed_to_play(oldest_enqueued_media_request)

    def take_action_based_on_image_media_requests_in_db(self):
        media_type = BotMediaRequestMediaTypes.IMAGE

        # Get all enqueued image media requests for this bot, ordered by creation time
        enqueued_requests = self.bot_in_db.media_requests.filter(state=BotMediaRequestStates.ENQUEUED, media_type=media_type).order_by("created_at")

        if not enqueued_requests.exists():
            return

        # Get the most recently created request
        most_recent_request = enqueued_requests.last()

        # Mark the most recent request as FINISHED
        try:
            BotMediaRequestManager.set_media_request_playing(most_recent_request)
            self.adapter.send_raw_image(most_recent_request.media_blob.blob)
            BotMediaRequestManager.set_media_request_finished(most_recent_request)
        except Exception as e:
            logger.info(f"Error sending raw image: {e}")
            BotMediaRequestManager.set_media_request_failed_to_play(most_recent_request)

        # Mark all other enqueued requests as DROPPED
        for request in enqueued_requests.exclude(id=most_recent_request.id):
            BotMediaRequestManager.set_media_request_dropped(request)

    def take_action_based_on_video_media_requests_in_db(self):
        media_type = BotMediaRequestMediaTypes.VIDEO
        oldest_enqueued_media_request = self.bot_in_db.media_requests.filter(state=BotMediaRequestStates.ENQUEUED, media_type=media_type).order_by("created_at").first()
        if not oldest_enqueued_media_request:
            return
        currently_playing_media_request = self.bot_in_db.media_requests.filter(state=BotMediaRequestStates.PLAYING, media_type=media_type).first()
        if currently_playing_media_request:
            logger.info(f"Currently playing video media request {currently_playing_media_request.id} so cannot play another video media request")
            return

        try:
            BotMediaRequestManager.set_media_request_playing(oldest_enqueued_media_request)
            self.video_output_manager.start_playing_video_media_request(oldest_enqueued_media_request)
        except Exception as e:
            logger.info(f"Error playing video media request: {e}")
            BotMediaRequestManager.set_media_request_failed_to_play(oldest_enqueued_media_request)

    def take_action_based_on_media_requests_in_db(self):
        self.take_action_based_on_audio_media_requests_in_db()
        self.take_action_based_on_image_media_requests_in_db()
        self.take_action_based_on_video_media_requests_in_db()

    def handle_glib_shutdown(self):
        logger.info("handle_glib_shutdown called")

        try:
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.FATAL_ERROR,
                event_sub_type=BotEventSubTypes.FATAL_ERROR_PROCESS_TERMINATED,
            )
        except Exception as e:
            logger.info(f"Error creating FATAL_ERROR event: {e}")

        self.cleanup()
        return False

    def handle_redis_message(self, message):
        if message and message["type"] == "message":
            data = json.loads(message["data"].decode("utf-8"))
            command = data.get("command")

            if command == "sync":
                logger.info(f"Syncing bot {self.bot_in_db.object_id}")
                self.bot_in_db.refresh_from_db()
                self.take_action_based_on_bot_in_db()
            elif command == "sync_media_requests":
                logger.info(f"Syncing media requests for bot {self.bot_in_db.object_id}")
                self.bot_in_db.refresh_from_db()
                self.take_action_based_on_media_requests_in_db()
            else:
                logger.info(f"Unknown command: {command}")

    def set_bot_heartbeat(self):
        if self.bot_in_db.last_heartbeat_timestamp is None or self.bot_in_db.last_heartbeat_timestamp <= int(timezone.now().timestamp()) - 60:
            self.bot_in_db.set_heartbeat()

    def on_main_loop_timeout(self):
        try:
            if self.first_timeout_call:
                logger.info("First timeout call - taking initial action")
                self.bot_in_db.refresh_from_db()
                self.take_action_based_on_bot_in_db()
                self.first_timeout_call = False

            # Set heartbeat
            self.set_bot_heartbeat()

            # Process audio chunks
            self.per_participant_non_streaming_audio_input_manager.process_chunks()

            # Monitor transcription
            self.per_participant_streaming_audio_input_manager.monitor_transcription()

            # Process captions
            self.closed_caption_manager.process_captions()

            # Check if auto-leave conditions are met
            self.adapter.check_auto_leave_conditions()

            # Process audio output
            self.audio_output_manager.monitor_currently_playing_audio_media_request()

            # Process video output
            self.video_output_manager.monitor_currently_playing_video_media_request()
            return True

        except Exception as e:
            logger.info(f"Error in timeout callback: {e}")
            logger.info("Traceback:")
            logger.info(traceback.format_exc())
            self.cleanup()
            return False

    def get_recording_in_progress(self):
        recordings_in_progress = Recording.objects.filter(bot=self.bot_in_db, state=RecordingStates.IN_PROGRESS)
        if recordings_in_progress.count() == 0:
            return None
        if recordings_in_progress.count() > 1:
            raise Exception(f"Expected at most one recording in progress for bot {self.bot_in_db.object_id}, but found {recordings_in_progress.count()}")
        return recordings_in_progress.first()

    def save_closed_caption_utterance(self, message):
        participant, _ = Participant.objects.get_or_create(
            bot=self.bot_in_db,
            uuid=message["participant_uuid"],
            defaults={
                "user_uuid": message["participant_user_uuid"],
                "full_name": message["participant_full_name"],
            },
        )

        # Create new utterance record
        recording_in_progress = self.get_recording_in_progress()
        if recording_in_progress is None:
            logger.warning(f"Warning: No recording in progress found so cannot save closed caption utterance. Message: {message}")
            return
        source_uuid = f"{recording_in_progress.object_id}-{message['source_uuid_suffix']}"
        utterance, _ = Utterance.objects.update_or_create(
            recording=recording_in_progress,
            source_uuid=source_uuid,
            defaults={
                "source": Utterance.Sources.CLOSED_CAPTION_FROM_PLATFORM,
                "participant": participant,
                "transcription": {"transcript": message["text"]},
                "timestamp_ms": message["timestamp_ms"],
                "duration_ms": message["duration_ms"],
                "sample_rate": None,
            },
        )

        RecordingManager.set_recording_transcription_in_progress(recording_in_progress)

    def save_individual_audio_utterance(self, message):
        from bots.tasks.process_utterance_task import process_utterance

        logger.info("Received message that new utterance was detected")

        # Create participant record if it doesn't exist
        participant, _ = Participant.objects.get_or_create(
            bot=self.bot_in_db,
            uuid=message["participant_uuid"],
            defaults={
                "user_uuid": message["participant_user_uuid"],
                "full_name": message["participant_full_name"],
            },
        )

        # Create new utterance record
        recording_in_progress = self.get_recording_in_progress()
        if recording_in_progress is None:
            logger.warning("Warning: No recording in progress found so cannot save individual audio utterance.")
            return
        utterance = Utterance.objects.create(
            source=Utterance.Sources.PER_PARTICIPANT_AUDIO,
            recording=recording_in_progress,
            participant=participant,
            audio_blob=message["audio_data"],
            audio_format=Utterance.AudioFormat.PCM,
            timestamp_ms=message["timestamp_ms"],
            duration_ms=len(message["audio_data"]) / 64,
            sample_rate=message["sample_rate"],
        )

        # Process the utterance immediately
        process_utterance.delay(utterance.id)
        return

    def on_message_from_adapter(self, message):
        GLib.idle_add(lambda: self.take_action_based_on_message_from_adapter(message))

    def flush_utterances(self):
        if self.per_participant_non_streaming_audio_input_manager:
            logger.info("Flushing utterances...")
            self.per_participant_non_streaming_audio_input_manager.flush_utterances()
        if self.closed_caption_manager:
            logger.info("Flushing captions...")
            self.closed_caption_manager.flush_captions()

    def save_debug_recording(self):
        # Only save if the file exists
        if not os.path.exists(BotAdapter.DEBUG_RECORDING_FILE_PATH):
            logger.info(f"Debug recording file at {BotAdapter.DEBUG_RECORDING_FILE_PATH} does not exist, not saving")
            return

        # Find the bot's last event
        last_bot_event = self.bot_in_db.last_bot_event()
        if last_bot_event:
            debug_screenshot = BotDebugScreenshot.objects.create(bot_event=last_bot_event)

            # Save the file directly from the file path
            with open(BotAdapter.DEBUG_RECORDING_FILE_PATH, "rb") as f:
                debug_screenshot.file.save(f"debug_screen_recording_{debug_screenshot.object_id}.mp4", f, save=True)
            logger.info(f"Saved debug recording with ID {debug_screenshot.object_id}")

    def take_action_based_on_message_from_adapter(self, message):
        if message.get("message") == BotAdapter.Messages.REQUEST_TO_JOIN_DENIED:
            logger.info("Received message that request to join was denied")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_REQUEST_TO_JOIN_DENIED,
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.MEETING_NOT_FOUND:
            logger.info("Received message that meeting not found")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_MEETING_NOT_FOUND,
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.BLOCKED_BY_GOOGLE_REPEATEDLY:
            from bots.tasks.restart_bot_pod_task import restart_bot_pod

            if self.bot_in_db.created_at < timezone.now() - timedelta(minutes=15):
                logger.info("Received message that we were blocked by google repeatedly but bot was created more than 15 minutes ago, so not recreating pod")

                new_bot_event = BotEventManager.create_event(
                    bot=self.bot_in_db,
                    event_type=BotEventTypes.FATAL_ERROR,
                    event_sub_type=BotEventSubTypes.FATAL_ERROR_UI_ELEMENT_NOT_FOUND,
                    event_metadata={
                        "bot_restarts_exceeded_max_retries": True,
                    },
                )
                self.cleanup()
                return

            logger.info("Received message that we were blocked by google repeatedly, so recreating pod")
            # Run task to restart the bot pod with 1 minute delay
            restart_bot_pod.apply_async(args=[self.bot_in_db.id], countdown=60)
            # Don't do the normal cleanup tasks because we'll be restarting the pod
            if self.main_loop and self.main_loop.is_running():
                logger.info("Quitting main loop")
                self.main_loop.quit()
            return

        if message.get("message") == BotAdapter.Messages.UI_ELEMENT_NOT_FOUND:
            logger.info(f"Received message that UI element not found at {message.get('current_time')}")

            screenshot_available = message.get("screenshot_path") is not None
            mhtml_file_available = message.get("mhtml_file_path") is not None

            new_bot_event = BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.FATAL_ERROR,
                event_sub_type=BotEventSubTypes.FATAL_ERROR_UI_ELEMENT_NOT_FOUND,
                event_metadata={
                    "step": message.get("step"),
                    "current_time": message.get("current_time").isoformat(),
                    "exception_type": message.get("exception_type"),
                    "exception_message": message.get("exception_message"),
                    "inner_exception_type": message.get("inner_exception_type"),
                    "inner_exception_message": message.get("inner_exception_message"),
                },
            )

            if screenshot_available:
                # Create debug screenshot
                debug_screenshot = BotDebugScreenshot.objects.create(bot_event=new_bot_event)

                # Read the file content from the path
                with open(message.get("screenshot_path"), "rb") as f:
                    screenshot_content = f.read()
                    debug_screenshot.file.save(
                        f"debug_screenshot_{debug_screenshot.object_id}.png",
                        ContentFile(screenshot_content),
                        save=True,
                    )

            if mhtml_file_available:
                # Create debug screenshot
                mhtml_debug_screenshot = BotDebugScreenshot.objects.create(bot_event=new_bot_event)

                with open(message.get("mhtml_file_path"), "rb") as f:
                    mhtml_content = f.read()
                    mhtml_debug_screenshot.file.save(
                        f"debug_screenshot_{mhtml_debug_screenshot.object_id}.mhtml",
                        ContentFile(mhtml_content),
                        save=True,
                    )

            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.ADAPTER_REQUESTED_BOT_LEAVE_MEETING:
            logger.info(f"Received message that adapter requested bot leave meeting reason={message.get('leave_reason')}")

            event_sub_type_for_reason = {
                BotAdapter.LEAVE_REASON.AUTO_LEAVE_SILENCE: BotEventSubTypes.LEAVE_REQUESTED_AUTO_LEAVE_SILENCE,
                BotAdapter.LEAVE_REASON.AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING: BotEventSubTypes.LEAVE_REQUESTED_AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING,
            }[message.get("leave_reason")]

            BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.LEAVE_REQUESTED, event_sub_type=event_sub_type_for_reason)
            BotEventManager.set_requested_bot_action_taken_at(self.bot_in_db)
            self.adapter.leave()
            return

        if message.get("message") == BotAdapter.Messages.MEETING_ENDED:
            logger.info("Received message that meeting ended")
            self.flush_utterances()
            if self.bot_in_db.state == BotStates.LEAVING:
                BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.BOT_LEFT_MEETING)
            else:
                BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.MEETING_ENDED)
            self.cleanup()

            return

        if message.get("message") == BotAdapter.Messages.ZOOM_MEETING_STATUS_FAILED_UNABLE_TO_JOIN_EXTERNAL_MEETING:
            logger.info(f"Received message that meeting status failed unable to join external meeting with zoom_result_code={message.get('zoom_result_code')}")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_UNPUBLISHED_ZOOM_APP,
                event_metadata={"zoom_result_code": str(message.get("zoom_result_code"))},
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.ZOOM_MEETING_STATUS_FAILED:
            logger.info(f"Received message that meeting status failed with zoom_result_code={message.get('zoom_result_code')}")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_MEETING_STATUS_FAILED,
                event_metadata={"zoom_result_code": str(message.get("zoom_result_code"))},
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.ZOOM_AUTHORIZATION_FAILED:
            logger.info(f"Received message that authorization failed with zoom_result_code={message.get('zoom_result_code')}")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_AUTHORIZATION_FAILED,
                event_metadata={"zoom_result_code": str(message.get("zoom_result_code"))},
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.ZOOM_SDK_INTERNAL_ERROR:
            logger.info(f"Received message that SDK internal error with zoom_result_code={message.get('zoom_result_code')}")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_SDK_INTERNAL_ERROR,
                event_metadata={"zoom_result_code": str(message.get("zoom_result_code"))},
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.LEAVE_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED:
            logger.info("Received message to leave meeting because waiting room timeout exceeded")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED,
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.LEAVE_MEETING_WAITING_FOR_HOST:
            logger.info("Received message to Leave meeting because received waiting for host status")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_NOT_STARTED_WAITING_FOR_HOST,
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.BOT_PUT_IN_WAITING_ROOM:
            logger.info("Received message to put bot in waiting room")
            BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.BOT_PUT_IN_WAITING_ROOM)
            return

        if message.get("message") == BotAdapter.Messages.BOT_JOINED_MEETING:
            logger.info("Received message that bot joined meeting")
            BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.BOT_JOINED_MEETING)
            return

        if message.get("message") == BotAdapter.Messages.READY_TO_SHOW_BOT_IMAGE:
            logger.info("Received message that bot is ready to show image")
            # If there are any image media requests, this will start playing them
            # For now the only type of media request is an image, so this will start showing the bot's image
            self.take_action_based_on_image_media_requests_in_db()
            return

        if message.get("message") == BotAdapter.Messages.BOT_RECORDING_PERMISSION_GRANTED:
            logger.info("Received message that bot recording permission granted")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
            )
            return

        raise Exception(f"Received unexpected message from bot adapter: {message}")
