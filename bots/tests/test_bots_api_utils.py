from django.test import TestCase

from accounts.models import Organization
from bots.bots_api_utils import create_bot, validate_meeting_url_and_credentials
from bots.models import Project


class TestValidateMeetingUrlAndCredentials(TestCase):
    def setUp(self):
        # Create organization first since it's required for Project
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)

    def test_validate_google_meet_url(self):
        """Test Google Meet URL validation"""
        # Valid Google Meet URL
        error = validate_meeting_url_and_credentials("https://meet.google.com/abc-defg-hij", self.project)
        self.assertIsNone(error)

    def test_validate_zoom_url_and_credentials(self):
        """Test Zoom URL and credentials validation"""
        # Test Zoom URL without credentials
        error = validate_meeting_url_and_credentials("https://zoom.us/j/123456789", self.project)
        self.assertEqual(error, {"error": f"Zoom App credentials are required to create a Zoom bot. Please add Zoom credentials at https://app.attendee.dev/projects/{self.project.object_id}/credentials"})

    def test_validate_teams_url(self):
        """Test Teams URL validation"""
        # Teams URLs don't require specific validation
        error = validate_meeting_url_and_credentials("https://teams.microsoft.com/meeting/123", self.project)
        self.assertIsNone(error)


class TestCreateBot(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)

    def test_create_bot(self):
        bot, error = create_bot({"meeting_url": "https://meet.google.com/abc-defg-hij", "bot_name": "Test Bot"}, self.project)
        self.assertIsNotNone(bot)
        self.assertIsNotNone(bot.recordings.first())
        self.assertIsNone(error)

    def test_create_bot_with_image(self):
        bot, error = create_bot({"meeting_url": "https://teams.microsoft.com/meeting/123", "bot_name": "Test Bot", "bot_image": {"type": "image/png", "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}}, self.project)
        self.assertIsNotNone(bot)
        self.assertIsNotNone(bot.recordings.first())
        self.assertIsNotNone(bot.media_requests.first())
        self.assertIsNone(error)

    def create_bot_with_google_meet_url_with_http(self):
        bot, error = create_bot({"meeting_url": "http://meet.google.com/abc-defg-hij", "bot_name": "Test Bot"}, self.project)
        self.assertIsNotNone(bot)
        self.assertIsNotNone(error)
        self.assertEqual(error, {"error": "Google Meet URL must start with https://meet.google.com/"})
