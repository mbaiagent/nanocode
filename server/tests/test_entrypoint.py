"""平台入口 nanocode-server：配置错误给人话 + 指向本地单机模式；all 是默认一体启动的别名。"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

from nanocode_server.__main__ import main


class EntrypointTest(unittest.TestCase):
    def run_main(self, argv):
        env = {k: v for k, v in os.environ.items() if not k.startswith("NCS_")}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            code = main(argv)
        return code, err.getvalue()

    def test_missing_auth_is_a_clean_error_pointing_to_local_mode(self):
        for argv in ([], ["standalone"], ["all"], ["control"]):
            code, err = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("NCS_AUTH", err)
            self.assertIn("nanocode --web", err)

    def test_unknown_command_prints_usage(self):
        code, _ = self.run_main(["bogus"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
