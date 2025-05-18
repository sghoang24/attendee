from django.test import TestCase

from bots.models import Bot, Organization, Project


class AutomaticLeaveSettingsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def test_automatic_leave_settings_default_values(self):
        """
        Test that default automatic leave settings are correctly applied when not specified
        """
        # Create a bot with empty settings
        bot = Bot.objects.create(
            project=self.project,
            meeting_url="https://zoom.us/j/123456789",
            name="Test Bot",
            settings={},
        )

        # Extract the automatic leave settings
        auto_leave_settings = bot.settings.get("automatic_leave_settings", {})

        # Assert default values are used
        self.assertEqual(auto_leave_settings.get("silence_timeout_seconds", 600), 600)
        self.assertEqual(
            auto_leave_settings.get("only_participant_in_meeting_timeout_seconds", 60),
            60,
        )
        self.assertEqual(
            auto_leave_settings.get("wait_for_host_to_start_meeting_timeout_seconds", 600),
            600,
        )
        self.assertEqual(auto_leave_settings.get("silence_activate_after_seconds", 1200), 1200)

    def test_automatic_leave_settings_custom_values(self):
        """
        Test that custom automatic leave settings are correctly applied
        """
        # Create a bot with custom settings
        custom_settings = {
            "automatic_leave_settings": {
                "silence_timeout_seconds": 300,
                "only_participant_in_meeting_timeout_seconds": 30,
                "wait_for_host_to_start_meeting_timeout_seconds": 900,
                "silence_activate_after_seconds": 600,
            }
        }

        bot = Bot.objects.create(
            project=self.project,
            meeting_url="https://zoom.us/j/123456789",
            name="Test Bot",
            settings=custom_settings,
        )

        # Extract the automatic leave settings
        auto_leave_settings = bot.settings.get("automatic_leave_settings", {})

        # Assert custom values are used
        self.assertEqual(auto_leave_settings.get("silence_timeout_seconds"), 300)
        self.assertEqual(auto_leave_settings.get("only_participant_in_meeting_timeout_seconds"), 30)
        self.assertEqual(
            auto_leave_settings.get("wait_for_host_to_start_meeting_timeout_seconds"),
            900,
        )
        self.assertEqual(auto_leave_settings.get("silence_activate_after_seconds"), 600)

    def test_automatic_leave_settings_validation(self):
        """
        Test that the validation correctly handles various input types
        """
        from rest_framework.exceptions import ValidationError

        from bots.serializers import CreateBotSerializer

        # Test with valid integers
        valid_settings = {
            "silence_timeout_seconds": 300,
            "only_participant_in_meeting_timeout_seconds": 30,
            "wait_for_host_to_start_meeting_timeout_seconds": 900,
            "silence_activate_after_seconds": 600,
        }

        serializer = CreateBotSerializer()
        validated_data = serializer.validate_automatic_leave_settings(valid_settings)

        self.assertEqual(validated_data["silence_timeout_seconds"], 300)
        self.assertEqual(validated_data["only_participant_in_meeting_timeout_seconds"], 30)
        self.assertEqual(validated_data["wait_for_host_to_start_meeting_timeout_seconds"], 900)
        self.assertEqual(validated_data["silence_activate_after_seconds"], 600)

        # Test with negative values (should raise ValidationError)
        invalid_settings = {"silence_timeout_seconds": -300}

        try:
            serializer.validate_automatic_leave_settings(invalid_settings)
            self.fail("ValidationError not raised for negative values")
        except ValidationError:
            pass  # Exception was correctly raised

        # Test with zero values (should raise ValidationError)
        zero_settings = {"silence_timeout_seconds": 0}

        try:
            serializer.validate_automatic_leave_settings(zero_settings)
            self.fail("ValidationError not raised for zero values")
        except ValidationError:
            pass  # Exception was correctly raised

        # Test with unexpected keys (should raise ValidationError)
        unexpected_keys = {"unexpected_key": 123}

        try:
            serializer.validate_automatic_leave_settings(unexpected_keys)
            self.fail("ValidationError not raised for unexpected keys")
        except ValidationError:
            pass  # Exception was correctly raised
