"""流式响应开始后出错（半截流、读超时、非 JSON 数据块）要转成 LLMError（#139）。"""
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler

from nanocode.llm.base import LLMError
from nanocode.llm.openai_compat import OpenAICompatProvider
from tests.helpers import LocalHTTPServer

CHUNK = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
MSG = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]


class _Server:
    def __init__(self, mode):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["content-length"]))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                if mode == "stall":
                    self.end_headers()
                    self.wfile.write(CHUNK.encode())   # 先发一个正常块，然后停住超过客户端超时
                    self.wfile.flush()
                    time.sleep(2.5)
                    return
                body = ("data: keep-alive\n\n" + CHUNK + "data: [DONE]\n\n").encode()
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = LocalHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class StreamErrorTest(unittest.TestCase):
    def _provider(self, mode, timeout=5):
        srv = _Server(mode)
        self.addCleanup(srv.close)
        return OpenAICompatProvider("m", base_url=srv.url, api_key="k", timeout=timeout, max_retries=0)

    def test_read_timeout_mid_stream_raises_llm_error(self):
        with self.assertRaises(LLMError) as cm:
            self._provider("stall", timeout=1).complete("", MSG, [])
        self.assertIn("流式响应中断", str(cm.exception))

    def test_non_json_data_line_is_skipped(self):
        r = self._provider("nonjson").complete("", MSG, [])
        self.assertIn("hi", str(r))


if __name__ == "__main__":
    unittest.main()
