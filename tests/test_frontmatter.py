"""技能 / 自定义命令的 frontmatter 要能解析 YAML 块标量（`>`、`|-` 等）。对应 issue #109。

官方技能库里 claude-api、academy-guide、discernment-nudge 用块标量写 description，
以前被解析成 ">" / "|-" 本身，续行还会变成假键，技能永远不会被模型触发。
"""

import os
import unittest

from helpers import Workspace

from nanocode.context import _parse_frontmatter, build_system_prompt, discover_commands, discover_skills


def fm(block: str) -> dict:
    return _parse_frontmatter("---\n" + block + "---\nbody\n")[0]


class BlockScalarTest(unittest.TestCase):
    def test_literal_strip(self):
        meta = fm("name: claude-api\ndescription: |-\n  Reference for the Claude API: model ids\n  second line\n")
        self.assertEqual(meta, {"name": "claude-api",
                                "description": "Reference for the Claude API: model ids\nsecond line"})

    def test_folded_clip(self):
        meta = fm("name: academy-guide\ndescription: >\n  Folded one\n  folded two\n\n  next para\nother: x\n")
        self.assertEqual(meta["description"], "Folded one folded two\nnext para\n")
        self.assertEqual(meta["other"], "x")
        self.assertEqual(set(meta), {"name", "description", "other"})     # 续行不会变成假键

    def test_literal_keep_and_folded_strip(self):
        self.assertEqual(fm("description: |\n  a\n\n  b\n")["description"], "a\n\nb\n")
        self.assertEqual(fm("description: |+\n  a\n\n")["description"], "a\n\n")
        self.assertEqual(fm("description: >-\n  a\n  b\n")["description"], "a b")

    def test_folded_keeps_more_indented_lines(self):
        self.assertEqual(fm("description: >-\n  a\n    deeper\n  b\n")["description"], "a\n  deeper\nb")
        self.assertEqual(fm("description: >\n  a\n\n    deep\n\n  b\n")["description"], "a\n\n  deep\n\nb\n")

    def test_plain_value_with_continuation(self):
        self.assertEqual(fm("description: plain\n  continued here\n")["description"], "plain continued here")

    def test_single_line_and_quoted_unchanged(self):
        meta = fm('name: "findskill"\ndescription: \'one line: with colon\'\nversion: 1\n')
        self.assertEqual(meta, {"name": "findskill", "description": "one line: with colon", "version": "1"})

    def test_body_returned_and_no_frontmatter(self):
        self.assertEqual(_parse_frontmatter("---\ndescription: >\n  x\n---\n\nhello\n"), ({"description": "x\n"}, "hello\n"))
        self.assertEqual(_parse_frontmatter("no frontmatter"), ({}, "no frontmatter"))


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.cleanup()

    def _write(self, rel: str, text: str):
        path = os.path.join(self.ws.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_skill_description_block_scalar_reaches_prompt_on_one_line(self):
        self._write(".nanocode/skills/claude-api/SKILL.md",
                    "---\nname: claude-api\ndescription: |-\n  Reference for the Claude API.\n  Use when coding.\n---\n# body\n")
        skills = discover_skills(self.ws.dir)
        self.assertEqual(skills["claude-api"]["description"], "Reference for the Claude API.\nUse when coding.")
        prompt = build_system_prompt(self.ws.dir, skills=skills)
        self.assertIn("- claude-api：Reference for the Claude API. Use when coding.", prompt)
        self.assertNotIn("：|-", prompt)

    def test_command_description_block_scalar(self):
        self._write(".nanocode/commands/review.md", "---\ndescription: >-\n  Review the\n  current diff\n---\nReview $ARGUMENTS\n")
        cmd = discover_commands(self.ws.dir)["review"]
        self.assertEqual(cmd["description"], "Review the current diff")
        self.assertEqual(cmd["template"], "Review $ARGUMENTS\n")


if __name__ == "__main__":
    unittest.main()
