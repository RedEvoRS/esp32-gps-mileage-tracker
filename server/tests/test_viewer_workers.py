"""Exercise the deployed worker arguments against browser pre-opened sockets."""

import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from werkzeug.security import generate_password_hash


VIEWER = Path(__file__).resolve().parent.parent / 'mileage-viewer'


def worker_command():
    # Read the real CMD so a Dockerfile regression also changes the live test.
    text = (VIEWER / 'Dockerfile').read_text(encoding='utf-8')
    return json.loads(re.search(r'^CMD (\[.*\])', text, re.M | re.S)[1].replace('\\\n', ''))


class ViewerWorkerConfigTests(unittest.TestCase):
    def test_viewer_uses_threaded_workers(self):
        command = worker_command()
        self.assertEqual(command[command.index('--workers') + 1], '2')
        self.assertEqual(command[command.index('--worker-class') + 1], 'gthread')
        self.assertGreaterEqual(int(command[command.index('--threads') + 1]), 2)


@unittest.skipUnless(sys.platform.startswith('linux'), 'gunicorn requires Linux/fcntl')
class ViewerWorkerIntegrationTests(unittest.TestCase):
    def test_phone_preopened_connections_do_not_stall_or_kill_workers(self):
        # Two spare sockets block both old sync workers, but leave room in the
        # configured thread pools for a phone's actual request.
        self.run_idle_connections(saturated=False, response_limit=1.5)

    def test_saturated_threads_recover_without_worker_deaths(self):
        # Gunicorn 26.2.0 waits up to 5 s before deferring a new idle socket
        # to its selector. Accepts need not be balanced: all nine spare sockets
        # can land on one worker, requiring two waves before a request runs.
        # Local runs measured 8-10 s; the bound leaves room for slower CI
        # runners; what matters is recovery without any worker being killed.
        self.run_idle_connections(saturated=True, response_limit=20)

    def run_idle_connections(self, *, saturated, response_limit):
        command = worker_command()
        workers = int(command[command.index('--workers') + 1])
        threads = int(command[command.index('--threads') + 1]) if '--threads' in command else 1
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        command[command.index('--bind') + 1] = f'127.0.0.1:{port}'
        command[0:1] = [sys.executable, '-m', 'gunicorn']
        command += ['--timeout', '5', '--graceful-timeout', '2']

        def health():
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=response_limit)
            try:
                connection.request('GET', '/health', headers={'Connection': 'close'})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            env = os.environ.copy()
            env.update({
                'INGEST_DATA_ROOT': str(root / 'data'),
                'VIEWER_DB_PATH': str(root / 'annotations.db'),
                'VIEWER_USERNAME': 'admin',
                'VIEWER_PASSWORD_HASH': generate_password_hash('example-password'),
                'VIEWER_SECRET_KEY': 'example-session-key-' * 4,
                'DISPLAY_TZ': 'UTC', 'VIEWER_GEOCODER_URL': '',
                'VIEWER_GEOCODER_INTERVAL': '', 'VIEWER_GEOCODER_AUTO_LOOKUP': '',
            })
            idle = []
            with (root / 'gunicorn.log').open('w+') as log:
                process = subprocess.Popen(command, cwd=VIEWER, env=env, stdout=log, stderr=log)
                try:
                    # Generous: a busy CI runner can be slow to import the app.
                    startup_deadline = time.monotonic() + 20
                    while True:
                        self.assertIsNone(process.poll(), 'gunicorn exited during startup')
                        try:
                            health()
                            break
                        except OSError:
                            if time.monotonic() >= startup_deadline:
                                self.fail('gunicorn did not become healthy within 20 seconds')
                            time.sleep(0.05)
                    idle_count = workers * threads + 1 if saturated else 2
                    for _ in range(idle_count):
                        idle.append(socket.create_connection(('127.0.0.1', port), timeout=1))
                    time.sleep(0.3)  # Let workers accept the idle connections.
                    deadline = time.monotonic() + 6  # Beyond the worker timeout.
                    delays = []
                    while True:
                        started = time.monotonic()
                        try:
                            health()
                        except OSError as exc:
                            self.fail(f'/health stalled behind idle connections: {type(exc).__name__}')
                        delays.append(time.monotonic() - started)
                        self.assertLess(delays[-1], response_limit, '/health was too slow')
                        self.assertIsNone(process.poll(), 'gunicorn exited with idle connections')
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.25)
                finally:
                    for connection in idle:
                        connection.close()
                    process.terminate()
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                    log.seek(0)
                    output = log.read()
                print(f"Viewer {'saturation' if saturated else 'phone'}: "
                      f"{idle_count} idle sockets; max /health delay {max(delays):.3f} s")
                self.assertNotIn('WORKER TIMEOUT', output)
                self.assertNotIn('SIGKILL', output)
                self.assertEqual(process.returncode, 0, output)


if __name__ == '__main__':
    unittest.main()
