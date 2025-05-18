import json
import logging

import numpy as np

from bots.google_meet_bot_adapter.google_meet_ui_methods import (
    GoogleMeetUIMethods,
)
from bots.web_bot_adapter import WebBotAdapter

logger = logging.getLogger(__name__)


class GoogleMeetBotAdapter(WebBotAdapter, GoogleMeetUIMethods):
    def __init__(
        self,
        *args,
        google_meet_closed_captions_language: str | None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.google_meet_closed_captions_language = google_meet_closed_captions_language

    def get_chromedriver_payload_file_name(self):
        return "google_meet_bot_adapter/google_meet_chromedriver_payload.js"

    def get_websocket_port(self):
        return 8765

    def send_raw_audio(self, bytes, sample_rate):
        """
        Sends raw audio bytes to the Google Meet call.

        :param bytes: Raw audio bytes in PCM format
        :param sample_rate: Sample rate of the audio in Hz
        """
        if not self.driver:
            print("Cannot send audio - driver not initialized")
            return

        # Convert bytes to Int16Array for JavaScript
        audio_data = np.frombuffer(bytes, dtype=np.int16).tolist()

        # Call the JavaScript function to enqueue the PCM chunk
        self.driver.execute_script(f"window.botOutputManager.playPCMAudio({audio_data}, {sample_rate})")

    def is_sent_video_still_playing(self):
        result = self.driver.execute_script("return window.botOutputManager.isVideoPlaying();")
        logger.info(f"is_sent_video_still_playing result = {result}")
        return result

    def send_video(self, video_url):
        logger.info(f"send_video called with video_url = {video_url}")
        self.driver.execute_script(f"window.botOutputManager.playVideo({json.dumps(video_url)})")
