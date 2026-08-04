import unittest

from main import _first_user_title


class FirstUserTitleTests(unittest.TestCase):
    def test_uses_first_user_message_without_title_metadata(self):
        records = [
            {"type": "user", "message": {"content": "First session message"}},
            {"type": "user", "message": {"content": "Later message"}},
        ]

        self.assertEqual(_first_user_title(records), "First session message")

    def test_uses_claude_agent_name_title(self):
        records = [
            {"type": "user", "message": {"content": "First session message"}},
            {"type": "agent-name", "agentName": "Claude session title"},
        ]

        self.assertEqual(_first_user_title(records), "Claude session title")

    def test_uses_claude_ai_title(self):
        records = [
            {
                "type": "user",
                "isMeta": True,
                "message": {"content": "Caveat: internal command message"},
            },
            {"type": "ai-title", "aiTitle": "Check project memory storage"},
        ]

        self.assertEqual(_first_user_title(records), "Check project memory storage")

    def test_uses_viewer_custom_title(self):
        records = [
            {"type": "user", "message": {"content": "First session message"}},
            {"type": "custom-title", "customTitle": "Viewer session title"},
        ]

        self.assertEqual(_first_user_title(records), "Viewer session title")

    def test_uses_latest_title_metadata_record(self):
        records = [
            {"type": "agent-name", "agentName": "Initial Claude title"},
            {"type": "custom-title", "customTitle": "Viewer title"},
            {"type": "agent-name", "agentName": "Latest Claude title"},
        ]

        self.assertEqual(_first_user_title(records), "Latest Claude title")

    def test_ignores_meta_user_messages_when_using_fallback(self):
        records = [
            {
                "type": "user",
                "isMeta": True,
                "message": {"content": "Caveat: internal command message"},
            },
            {"type": "user", "message": {"content": "First human message"}},
        ]

        self.assertEqual(_first_user_title(records), "First human message")


if __name__ == "__main__":
    unittest.main()
