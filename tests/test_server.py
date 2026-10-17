import threading
import unittest
from http.server import HTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from store_monitor import server


class ServerTest(unittest.TestCase):
    def test_post_runs_one_check_and_reports_failure_as_500(self):
        httpd = HTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        url = f"http://127.0.0.1:{httpd.server_port}/run"

        def post(query=""):
            try:
                return urlopen(Request(url + query, method="POST")).status
            except HTTPError as error:
                return error.code

        with patch.object(server, "main", side_effect=[0, 1, RuntimeError("boom")]) as main:
            self.assertEqual(post(), 200)
            self.assertEqual(post("?mode=test-discord"), 500)
            self.assertEqual(post("?mode=dry-run"), 500)  # An exception also counts as a failed run.
            self.assertEqual(post("?mode=--local-state"), 400)  # Arbitrary flags are refused.
        self.assertEqual([call.args[0] for call in main.call_args_list], [[], ["--test-discord"], ["--dry-run"]])


if __name__ == "__main__":
    unittest.main()
