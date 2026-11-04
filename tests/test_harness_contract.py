"""Static contract tests; these do NOT claim the optional preset is installed."""
import json
from pathlib import Path
import unittest
from video_agent.engine import TASKS

ROOT = Path(__file__).resolve().parents[1]


class HarnessContractTests(unittest.TestCase):
    def test_project_skill_and_agent_exist(self):
        skill = (ROOT / ".dsh/skills/video-editing/SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: video-editing\n"))
        self.assertIn("description:", skill)
        self.assertIn("video-editing", (ROOT / "AGENTS.md").read_text())
        self.assertIn("python3 -m video_agent", skill)
        for task in TASKS:
            self.assertIn(task, skill)

    def test_bundle_metadata(self):
        bundle = ROOT / "harness"
        metadata = json.loads((bundle / "package.json").read_text())
        patch = bundle / metadata["dsh"]["bundle"]["patch"]
        self.assertTrue(patch.is_file())
        value = patch.read_text()
        self.assertIn("- insert:", value)
        self.assertIn("@deepseek-ai/dsh-agent-preset", value)
        self.assertIn("@deepseek-ai/dsh-persona", value)
        self.assertIn("prefix:", value)
        self.assertNotRegex(value, r"(?m)^\s+text:")

    def test_no_web_application(self):
        self.assertFalse((ROOT / "apps/web").exists())
        self.assertFalse((ROOT / "services/api").exists())


if __name__ == "__main__":
    unittest.main()
