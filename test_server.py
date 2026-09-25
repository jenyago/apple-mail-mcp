import json
import subprocess
import unittest
from unittest.mock import patch
import server

class BridgeTests(unittest.TestCase):
    def test_payload_is_data(self):
        hostile = '\"); throw Error("injected"); //\n🍎'
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')) as run:
            server.call_mail('search', query=hostile)
            script = run.call_args.kwargs['input']
            literal = script.split('JSON.parse(', 1)[1].split(');\n', 1)[0]
            self.assertEqual(json.loads(json.loads(literal))['query'], hostile)
            self.assertEqual(run.call_args.args[0], ['/usr/bin/osascript', '-l', 'JavaScript', '-'])

    def test_permission_error(self):
        with patch.object(server.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'denied (-1743)')):
            with self.assertRaisesRegex(RuntimeError, 'Automation'):
                server.list_accounts()

    def test_timeout(self):
        with patch.object(server.subprocess, 'run', side_effect=subprocess.TimeoutExpired('osascript', 45)):
            with self.assertRaisesRegex(RuntimeError, 'partially completed'):
                server.list_accounts()

    def test_invalid_recipient(self):
        with patch.object(server.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                server.create_draft(['x@example.com\nBcc:evil@example.com'], 'test', 'body')
            run.assert_not_called()

if __name__ == '__main__':
    unittest.main()
