"""会话保存要原子、读到坏文件要能容错、保存失败不能静默。对应 issue #8。"""

import errno
import json
import os
import unittest
from unittest import mock

from helpers import Workspace

import nanocode.session as sess
from nanocode.session import Options, Session


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


class _SessionCase(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.s = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0), log=lambda m: None)
        self.errors = []
        self.s.bus.subscribe(lambda e: self.errors.append(e) if e["type"] == "error" else None)
        self.dir = os.path.join(self.s.data_dir, "sessions")
        self.path = os.path.join(self.dir, f"{self.s.session_id}.json")

    def tearDown(self):
        self.s.close()
        self.ws.cleanup()

    def load(self, path=None):
        with open(path or self.path, encoding="utf-8") as f:
            return json.load(f)



class SessionSaveTest(_SessionCase):
    def test_failed_save_keeps_the_previous_file_intact(self):
        self.s.agent.messages = [user("第一轮")]
        self.s.save()
        self.s.agent.messages.append(user("第二轮"))

        def dying_dump(obj, f, **kw):      # 写到一半磁盘满了
            f.write('{"messages": [{"role": "us')
            f.flush()
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(sess.json, "dump", dying_dump):
            self.s.save()
        self.assertEqual(len(self.load()["messages"]), 1)            # 旧文件原封不动
        self.assertEqual(os.listdir(self.dir), [os.path.basename(self.path)])   # 临时文件已清理

    def test_failed_save_is_reported_not_swallowed(self):
        with mock.patch.object(sess.json, "dump", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            self.s.save()
        self.assertEqual(len(self.errors), 1)
        self.assertIn("会话保存失败", self.errors[0]["message"])
        self.assertIn("No space left", self.errors[0]["message"])

    def test_unserializable_content_does_not_crash_or_corrupt(self):
        self.s.agent.messages = [user("好的")]
        self.s.save()
        self.s.agent.messages.append({"role": "user", "content": object()})
        self.s.save()                                                 # 不抛异常
        self.assertEqual(len(self.load()["messages"]), 1)
        self.assertEqual(len(self.errors), 1)

    def test_successful_save_roundtrips(self):
        self.s.agent.messages = [user("你好")]
        self.s.agent.state["todos"] = [{"content": "x", "status": "pending"}]
        self.s.save()
        s2 = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0), log=lambda m: None)
        self.addCleanup(s2.close)
        self.assertIn("已恢复会话", s2.resume_latest())
        self.assertEqual(s2.agent.messages, [user("你好")])
        self.assertEqual(s2.agent.state["todos"], [{"content": "x", "status": "pending"}])


class ResumeCorruptTest(_SessionCase):
    def new_session(self):
        s = Session(Options(provider="mock", cwd=self.ws.dir, mock_delay=0), log=lambda m: None)
        self.addCleanup(s.close)
        return s

    def write(self, name, text):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_corrupt_file_is_backed_up_and_older_session_is_used(self):
        self.write("20260101-000000.json", json.dumps({"messages": [user("旧的好会话")], "todos": []}))
        self.write("20260102-000000.json", '{"messages": [{"role": "us')   # 最新的，但已损坏
        s = self.new_session()
        out = s.resume_latest()
        self.assertIn("已损坏", out)
        self.assertIn("20260102-000000.json.corrupt", out)
        self.assertIn("已恢复会话 20260101-000000", out)
        self.assertEqual(s.agent.messages, [user("旧的好会话")])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "20260102-000000.json.corrupt")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "20260102-000000.json")))

    def test_only_corrupt_files_reports_clearly_without_raising(self):
        self.write("20260101-000000.json", "not json at all")
        s = self.new_session()
        out = s.resume_latest()
        self.assertIn("已损坏", out)
        self.assertIn("没有可恢复的会话", out)

    def test_wrong_shape_counts_as_corrupt(self):
        self.write("20260101-000000.json", json.dumps([1, 2, 3]))
        self.write("20260102-000000.json", json.dumps({"messages": "oops"}))
        s = self.new_session()
        self.assertIn("没有可恢复的会话", s.resume_latest())

    def test_no_sessions_message_unchanged(self):
        s = self.new_session()
        self.assertEqual(s.resume_latest(), "没有可恢复的会话")

    # 空会话是切换 / 新建会话时 save() 留下的空壳，恢复出来是白板（#86）
    def test_empty_newest_session_is_skipped_for_the_last_real_one(self):
        self.write("20260101-000000.json", json.dumps({"messages": [user("有内容")], "todos": []}))
        self.write("20260102-000000.json", json.dumps({"messages": [], "todos": []}))
        s = self.new_session()
        out = s.resume_latest()
        self.assertIn("已恢复会话 20260101-000000", out)
        self.assertIn("跳过了 1 个空会话", out)
        self.assertEqual(s.agent.messages, [user("有内容")])

    def test_all_sessions_empty_reports_clearly_and_keeps_blank_state(self):
        self.write("20260101-000000.json", json.dumps({"messages": [], "todos": []}))
        s = self.new_session()
        before = s.session_id
        out = s.resume_latest()
        self.assertIn("没有可恢复的会话", out)
        self.assertIn("都是空的", out)
        self.assertEqual(s.agent.messages, [])
        self.assertEqual(s.session_id, before)         # 没把空会话当成"恢复成功"

    def test_empty_session_does_not_hide_an_older_corrupt_one(self):
        """空会话跳过、坏文件仍然备份，两件事不能互相干扰。"""
        self.write("20260101-000000.json", '{"messages": [{"role": "us')
        self.write("20260102-000000.json", json.dumps({"messages": [], "todos": []}))
        s = self.new_session()
        out = s.resume_latest()
        self.assertIn("已损坏", out)
        self.assertIn("没有可恢复的会话", out)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "20260101-000000.json.corrupt")))


if __name__ == "__main__":
    unittest.main()
