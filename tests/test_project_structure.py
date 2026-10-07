from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProjectStructureTests(unittest.TestCase):
    def test_required_entrypoints_exist(self) -> None:
        required = [
            "src/light_daily_screener.py",
            "src/daily_backtest_runner.py",
            "src/build_local_knowledge_index.py",
            "news_agent/financebot.py",
            "docs/strategy.md",
        ]
        for relative in required:
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_secret_files_are_ignored(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for item in (".env", "*.sqlite3", "knowledge_sources/"):
            self.assertIn(item, gitignore)

    def test_environment_template_has_placeholders_only(self) -> None:
        env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("replace_with_your_deepseek_key", env_text)
        self.assertNotIn("sk-", env_text)


if __name__ == "__main__":
    unittest.main()
