"""Credentialed reads never follow a redirect and never read an unbounded body.

Two loopback servers on different ports are two origins. Every token here is synthetic, and no
test reaches anything but 127.0.0.1.
"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import console_report
import usage_collector as uc

TOKEN = 'synthetic-test-token-0000000000000000'
ACCOUNT = 'synthetic-account-id'


class Origin:
    """One loopback origin: routes map a path to (status, headers, body, delay_s, drip_s, stall_s)."""

    def __init__(self):
        self.routes = {}
        self.seen = []
        origin = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                origin.seen.append({'path': self.path, 'headers': dict(self.headers)})
                status, headers, body, delay, drip, stall = origin.routes.get(
                    self.path.split('?')[0], (404, {}, b'{}', 0, 0, 0))
                time.sleep(delay)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if 'Content-Length' not in headers and 'Transfer-Encoding' not in headers:
                    self.send_header('Connection', 'close')  # close-delimited: no length at all
                    self.close_connection = True
                self.end_headers()
                try:
                    if stall:  # a byte late in the deadline, then silence
                        self.wfile.write(body[:1])
                        self.wfile.flush()
                        time.sleep(0.7)
                        self.wfile.write(body[1:2])
                        self.wfile.flush()
                        time.sleep(stall)
                        self.wfile.write(body[2:])
                    elif drip:
                        for byte in body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(drip)
                    else:
                        self.wfile.write(body)
                except OSError:
                    pass  # the client gave up first, which is the point of some tests

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self, path, status=200, body=b'{}', headers=None, length=True, delay=0, drip=0, stall=0):
        headers = dict(headers or {})
        if length:
            headers.setdefault('Content-Length', str(len(body)))
        headers.setdefault('Content-Type', 'application/json')
        self.routes[path] = (status, headers, body, delay, drip, stall)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = Origin(), Origin()
        self.b.route('/steal', body=b'{"stolen": true}')
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)

    def read(self, path, **kwargs):
        kwargs.setdefault('headers', {'ChatGPT-Account-Id': ACCOUNT})
        return uc.http_json(self.a.base + path, TOKEN, **kwargs)

    def assert_second_origin_untouched(self):
        self.assertEqual(self.b.seen, [], 'a second origin received a request')

    # ---- supported authentication
    def test_normal_response_carries_the_token_to_its_own_origin_only(self):
        self.a.route('/usage', body=b'{"usage": {"rolling": {"percent": 12}}}')
        self.assertEqual(self.read('/usage'), {'usage': {'rolling': {'percent': 12}}})
        self.assertEqual(len(self.a.seen), 1)
        self.assertEqual(self.a.seen[0]['headers']['Authorization'], 'Bearer ' + TOKEN)
        self.assert_second_origin_untouched()

    # ---- redirects
    def test_cross_origin_redirect_never_reaches_the_second_origin(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.a.route('/usage', status=code, headers={'Location': self.b.base + '/steal'}, body=b'')
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.read('/usage')
                self.assertEqual(caught.exception.code, code)
                self.assert_second_origin_untouched()

    def test_every_location_form_is_refused_at_the_first_hop(self):
        port_b = self.b.base.rsplit(':', 1)[1]
        locations = {
            'relative': '/elsewhere',
            'dot-relative': 'elsewhere',
            'scheme-relative': f'//127.0.0.1:{port_b}/steal',
            'https-to-http downgrade shape': f'http://127.0.0.1:{port_b}/steal',
            'self loop': '/usage',
            'malformed host': 'http://[::1/steal',
            'non-http scheme': 'file:///etc/passwd',
            'empty': '',
        }
        self.a.route('/elsewhere', body=b'{"followed": true}')
        for name, location in locations.items():
            with self.subTest(location=name):
                self.a.seen.clear()
                self.b.seen.clear()
                self.a.route('/usage', status=302, headers={'Location': location}, body=b'')
                with self.assertRaises(urllib.error.HTTPError):
                    self.read('/usage')
                self.assertEqual([r['path'] for r in self.a.seen], ['/usage'])
                self.assert_second_origin_untouched()

    def test_a_redirect_chain_stops_at_its_first_hop(self):
        self.a.route('/usage', status=302, headers={'Location': '/hop1'}, body=b'')
        self.a.route('/hop1', status=302, headers={'Location': self.b.base + '/steal'}, body=b'')
        with self.assertRaises(urllib.error.HTTPError):
            self.read('/usage')
        self.assertEqual([r['path'] for r in self.a.seen], ['/usage'])
        self.assert_second_origin_untouched()

    def test_refused_redirect_error_holds_no_credential_or_location(self):
        self.a.route('/usage', status=302, headers={'Location': self.b.base + '/steal'}, body=b'')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.read('/usage')
        text = f'{caught.exception} {caught.exception!r}'
        self.assertNotIn(TOKEN, text)
        self.assertNotIn('/steal', text)

    # ---- response size
    def limit(self, size):
        return patch.object(uc, 'MAX_RESPONSE_BYTES', size, create=True)

    def padded(self, size):
        """A valid JSON document of exactly `size` bytes."""
        body = b'{"p": "' + b'x' * (size - 9) + b'"}'
        self.assertEqual(len(body), size)
        return body

    def test_bodies_below_and_at_the_limit_are_read(self):
        with self.limit(1024):
            for size in (1023, 1024):
                with self.subTest(size=size):
                    self.a.route('/usage', body=self.padded(size))
                    self.assertEqual(len(self.read('/usage')['p']), size - 9)

    def test_a_body_one_byte_over_the_limit_is_refused(self):
        with self.limit(1024):
            self.a.route('/usage', body=self.padded(1025))
            with self.assertRaises(ValueError) as caught:
                self.read('/usage')
            self.assertIn('1024', str(caught.exception))
            self.assertNotIn('xxxx', str(caught.exception))

    def test_a_body_with_no_length_header_is_bounded_while_read(self):
        with self.limit(1024):
            self.a.route('/usage', body=self.padded(1024), length=False)
            self.assertEqual(len(self.read('/usage')['p']), 1015)
            self.a.route('/usage', body=self.padded(64 * 1024), length=False)
            with self.assertRaises(ValueError):
                self.read('/usage')

    def test_a_chunked_body_is_bounded_while_read(self):
        big = self.padded(64 * 1024)
        chunked = b'%x\r\n%s\r\n0\r\n\r\n' % (len(big), big)
        with self.limit(1024):
            self.a.route('/usage', body=chunked, length=False, headers={'Transfer-Encoding': 'chunked'})
            with self.assertRaises(ValueError):
                self.read('/usage')

    def test_a_declared_oversize_length_is_refused_without_reading(self):
        with self.limit(1024):
            self.a.route('/usage', body=b'{}', headers={'Content-Length': str(10 ** 12)}, length=False)
            with self.assertRaises(ValueError):
                self.read('/usage', timeout=2)

    def test_malformed_json_is_reported_without_its_body(self):
        self.a.route('/usage', body=b'<html>secret-body-marker</html>')
        with self.assertRaises(ValueError) as caught:
            self.read('/usage')
        self.assertIn('not JSON', str(caught.exception))
        self.assertNotIn('secret-body-marker', str(caught.exception))

    def test_http_errors_report_the_status_and_never_the_body(self):
        with self.limit(1024):
            self.a.route('/usage', status=500, body=b'secret-body-marker' * 4096)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.read('/usage')
            self.assertEqual(caught.exception.code, 500)
            self.assertNotIn('secret-body-marker', str(caught.exception))
            self.assertTrue(caught.exception.fp is None or caught.exception.fp.closed)

    # ---- timeouts
    def test_a_stalled_server_times_out(self):
        self.a.route('/usage', delay=1.5)
        started = time.monotonic()
        with self.assertRaises(OSError):  # URLError and TimeoutError are both OSErrors
            self.read('/usage', timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.4)

    def test_a_slow_drip_cannot_outlast_the_timeout(self):
        self.a.route('/usage', body=self.padded(200), drip=0.05)  # each byte well inside 0.3 s
        started = time.monotonic()
        with self.assertRaises(OSError):
            self.read('/usage', timeout=0.3)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_stall_after_the_first_bytes_ends_at_the_deadline(self):
        # A byte lands 0.7 s into a 1 s deadline, then the server stalls: the next receive gets
        # what is left of the deadline, not a fresh full socket timeout (which would end near 1.7 s).
        self.a.route('/usage', body=self.padded(200), stall=3.0)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.read('/usage', timeout=1.0)
        self.assertLess(time.monotonic() - started, 1.5)


class CollectorPathTest(unittest.TestCase):
    """The same guarantees through the paths that read real local credentials."""

    def setUp(self):
        self.a, self.b = Origin(), Origin()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)

    def test_collect_all_degrades_a_redirected_provider_without_leaking(self):
        auth = Path(self.home.name, '.local/share/opencode/auth.json')
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({'opencode-go': {'key': TOKEN}}))
        self.a.route('/usage', status=302, headers={'Location': self.b.base + '/steal'}, body=b'')
        env = {'HOME': self.home.name, 'OPENCODE_GO_BASE': self.a.base,
               'TMOS_USAGE_PROVIDERS_DIR': self.home.name}
        with patch.dict(os.environ, env):
            document = uc.collect_all(['opencode-go'], local_stats=False)
        row = document['providers'][0]
        self.assertEqual(row['status'], 'unknown')
        self.assertIn('HTTP 302', row['note'])
        self.assertNotIn(TOKEN, json.dumps(document))
        self.assertEqual(self.b.seen, [])

    def test_console_export_follows_no_redirect_and_keeps_the_last_snapshot(self):
        summary = Path(self.home.name, 'console-summary.json')
        summary.write_text('{"kept": true}\n')
        with patch.object(console_report, 'URL', self.a.base + '/export'), \
                patch.dict(os.environ, {'OPENCODE_CONSOLE_SERVICE_KEY': TOKEN}):
            self.a.route('/export', status=302, headers={'Location': self.b.base + '/steal'}, body=b'')
            with self.assertRaises(urllib.error.HTTPError):
                console_report.fetch(self.home.name)
            self.assertEqual(self.b.seen, [])
            with patch.object(console_report, 'MAX_BYTES', 1024):
                self.a.route('/export', body=b'a' * 4096, length=False, headers={'Content-Type': 'text/csv'})
                with self.assertRaises(ValueError):
                    console_report.fetch(self.home.name)
        self.assertEqual(summary.read_text(), '{"kept": true}\n')


if __name__ == '__main__':
    unittest.main()
