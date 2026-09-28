"""Fault-injection tests. No Lean, API calls, or user workspace files required."""
import io
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lea import diagnostics
from lea.check_runtime import CheckBudget, CheckFailure, lsp_disabled
from lea import lsp_daemon as lsp
from lea.lean_checks import check_file


class AutoTransport:
    def __init__(self, fail=False):
        self.sent = []
        self.closed = False
        self.fail = fail
        self.versions = {}
    def start(self, callback):
        self.callback = callback
        return True
    def send(self, msg):
        self.sent.append(msg)
        method = msg.get('method')
        if method == 'initialize':
            self.callback({'id': msg['id'], 'result': {}})
        if method in ('textDocument/didOpen', 'textDocument/didChange'):
            doc = msg['params']['textDocument']
            self.versions[doc['uri']] = doc['version']
        if method == 'textDocument/waitForDiagnostics':
            if self.fail:
                self.callback(None)
            else:
                p = msg['params']
                self.callback({'method': 'textDocument/publishDiagnostics', 'params': {
                    'uri': p['uri'], 'version': p['version'], 'diagnostics': []}})
                self.callback({'id': msg['id'], 'result': {}})
    def close(self): self.closed = True
    def poll(self): return None


class TransportTests(unittest.TestCase):
    def reader(self, data, chunk=3):
        class Fragments(io.BytesIO):
            def read(self, n=-1): return super().read(min(n, chunk))
        t = lsp._Transport([], '.')
        t._proc = SimpleNamespace(stdout=Fragments(data), poll=lambda: 0)
        got = []
        t._on_message = got.append
        t._reader()
        return t, got

    def test_fragmented_utf8_and_multiple_frames(self):
        frames = [{'id': 1, 'result': '∀ ε'}, {'id': 2, 'result': {}}]
        encoded = []
        for msg in frames:
            body = json.dumps(msg, ensure_ascii=False).encode('utf-8')
            encoded.append(f'Content-Length: {len(body)}\r\n\r\n'.encode() + body)
        _, got = self.reader(b''.join(encoded))
        self.assertEqual(got, [*frames, None])

    def test_headers_and_bodies_arrive_one_byte_at_a_time(self):
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, 'rb', buffering=0)
        t = lsp._Transport([], '.')
        t._proc = SimpleNamespace(stdout=stream, poll=lambda: 0)
        got = []
        t._on_message = got.append
        message = {'id': 7, 'result': {}}
        def write():
            try:
                for byte in lsp._encode(message):
                    os.write(write_fd, bytes([byte]))
            finally:
                os.close(write_fd)
        writer = threading.Thread(target=write)
        writer.start()
        try:
            t._reader()
            self.assertEqual(got, [message, None])
        finally:
            stream.close(); writer.join(1)

    def test_invalid_frames_fail_waiters(self):
        for data in (b'Content-Length: nope\r\n\r\n', b'Content-Length: 99\r\n\r\n{}',
                     b'Content-Length: 3\r\n\r\nbad', b'wat\r\n\r\n',
                     b'Content-Length: -1\r\n\r\n'):
            with self.subTest(data=data):
                t, got = self.reader(data)
                self.assertEqual(got, [None])
                self.assertEqual(t.failure.kind, 'protocol')

    def test_short_writes_complete(self):
        class Writer:
            def __init__(self): self.data = bytearray()
            def write(self, data):
                self.data.extend(data[:3]); return min(3, len(data))
        writer = Writer()
        t = lsp._Transport([], '.')
        t._proc = SimpleNamespace(stdin=writer)
        msg = {'id': 1, 'method': 'test'}
        t.send(msg)
        self.assertEqual(writer.data, lsp._encode(msg))

    def test_cancel_after_partial_write_invalidates_only_corrupted_transport(self):
        stopped = threading.Event()
        class Writer:
            def __init__(self): self.data = bytearray()
            def write(self, data):
                self.data.extend(data[:3])
                stopped.set()
                return 3
        t = lsp._Transport([], '.')
        writer = Writer()
        t._proc = SimpleNamespace(stdin=writer, poll=lambda: None)
        failures = []
        t._on_message = failures.append
        with self.assertRaises(CheckFailure) as error:
            t.send({'id': 1, 'method': 'test'}, budget=CheckBudget(1, stopped.is_set))
        self.assertEqual(error.exception.kind, 'cancelled')
        self.assertEqual(t.failure.kind, 'transport')
        self.assertEqual(failures, [None])
        with self.assertRaises(CheckFailure):
            t.send({'id': 2, 'method': 'test'})
        self.assertEqual(len(writer.data), 3)

        untouched = lsp._Transport([], '.')
        untouched._proc = SimpleNamespace(stdin=writer, poll=lambda: None)
        with self.assertRaises(CheckFailure):
            untouched.send({'id': 3}, budget=CheckBudget(1, stopped.is_set))
        self.assertIsNone(untouched.failure)

    def test_failed_handshake_closes_transport(self):
        t = AutoTransport()
        d = lsp.LeanDaemon('/tmp', lambda: t)
        with patch.object(d, '_request', side_effect=CheckFailure('initialization_timeout', 'timeout')):
            self.assertFalse(d.start())
        self.assertTrue(t.closed)
        self.assertTrue(d.closed.is_set())

    def test_stderr_is_bounded(self):
        t = lsp._Transport([], '.')
        t._drain_stderr(SimpleNamespace(stderr=io.BytesIO(b'x' * 65536)))
        self.assertEqual(len(t._stderr), 32768)

    def test_exact_version_required(self):
        d = lsp.LeanDaemon('/tmp', AutoTransport)
        for version in (None, 1, 3):
            q = Queue()
            q.put({'method': 'textDocument/publishDiagnostics',
                   'params': {'version': version, 'diagnostics': []}})
            with self.assertRaisesRegex(CheckFailure, 'no diagnostics'):
                d._collect(q, 2)
        q = Queue()
        q.put({'method': 'textDocument/publishDiagnostics', 'params': {'version': 2, 'diagnostics': []}})
        self.assertEqual(d._collect(q, 2), [])


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = str(Path(self.temp.name).resolve())
        self.file = Path(self.root, 'Example.lean')
        self.file.write_text('example : True := trivial\n')
        self.real_daemon = lsp.LeanDaemon
        self.created = []
    def tearDown(self):
        lsp._shutdown_all()
        lsp._roots.clear()
        lsp._daemons.clear()
        self.temp.cleanup()
    def factory(self, failures=0):
        def create(root):
            t = AutoTransport(fail=len(self.created) < failures)
            d = self.real_daemon(root, lambda: t)
            self.created.append(d)
            return d
        return create

    def test_recovery_before_cold_and_reuse(self):
        with patch.object(lsp, 'LeanDaemon', self.factory(1)):
            budget = CheckBudget(2)
            out = lsp.check_via_lsp(str(self.file), self.file.read_text(), self.root, budget=budget)
            self.assertIn('OK', out)
            self.assertEqual(budget.execution['attempts'], 2)
            self.assertEqual(lsp.runtime_snapshot(self.root)['state'], 'ready')
            lsp.check_via_lsp(str(self.file), self.file.read_text(), self.root)
            self.assertEqual(len(self.created), 2)
            self.assertTrue(self.created[0].closed.is_set())

    def test_later_outage_gets_a_new_retry_and_diagnostic_episode(self):
        token = diagnostics.begin_scope()
        try:
            with patch.object(lsp, 'LeanDaemon', self.factory(1)):
                lsp.check_via_lsp(str(self.file), 'x', self.root, budget=CheckBudget(2))
                self.created[-1]._transport.fail = True
                result = lsp.check_via_lsp(str(self.file), 'x', self.root, budget=CheckBudget(2))
                self.assertIn('OK', result)
                self.assertEqual(len(self.created), 3)
                episodes = [d.context['episode_id'] for d in diagnostics.drain()
                            if d.code == 'lean.lsp_recovering']
                self.assertEqual(len(set(episodes)), 2)
        finally:
            diagnostics.end_scope(token)

    def test_backoff_and_reset(self):
        with patch.object(lsp, 'LeanDaemon', self.factory(2)):
            for _ in range(2):
                with self.assertRaises(CheckFailure):
                    lsp.check_via_lsp(str(self.file), self.file.read_text(), self.root, budget=CheckBudget(2))
            self.assertEqual(len(self.created), 2)
            state = lsp._roots[self.root]
            self.assertEqual(state.failures, 1)
            state.retry_at = 0
            lsp.check_via_lsp(str(self.file), self.file.read_text(), self.root, budget=CheckBudget(2))
            self.assertEqual(state.failures, 0)
            self.assertEqual(lsp.runtime_snapshot(self.root)['state'], 'ready')

    def test_idle_server_failure_announces_recovery_before_startup(self):
        token = diagnostics.begin_scope()
        try:
            factory = self.factory()
            notices = []
            def create(root):
                daemon = factory(root)
                if len(self.created) == 2:
                    start = daemon.start
                    def observe_start(budget):
                        notices.extend(diagnostics.drain())
                        return start(budget)
                    daemon.start = observe_start
                return daemon
            with patch.object(lsp, 'LeanDaemon', create):
                lsp.check_via_lsp(str(self.file), 'x', self.root)
                self.created[0]._dispatch(None)
                lsp.check_via_lsp(str(self.file), 'x', self.root)
            self.assertEqual([d.code for d in notices], ['lean.lsp_recovering'])
        finally:
            diagnostics.end_scope(token)

    def test_concurrent_start_is_shared(self):
        with patch.object(lsp, 'LeanDaemon', self.factory()):
            errors = []
            def work(i):
                try: lsp.check_via_lsp(str(self.file.with_name(f'{i}.lean')), 'example : True := trivial', self.root, budget=CheckBudget(2))
                except Exception as exc: errors.append(exc)
            threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
            for t in threads: t.start()
            for t in threads: t.join(3)
            self.assertFalse(errors)
            self.assertTrue(all(not t.is_alive() for t in threads))
            self.assertEqual(len(self.created), 1)
            self.assertLessEqual(len(self.created[0].opened), 8)

    def test_first_document_does_not_block_other_documents_after_initialize(self):
        transport = AutoTransport()
        original = transport.send
        entered, cancel = threading.Event(), threading.Event()
        def wait_for_first(msg):
            if (msg.get('method') == 'textDocument/waitForDiagnostics'
                    and msg['params']['uri'] == self.file.as_uri()):
                entered.set()
            else:
                original(msg)
        transport.send = wait_for_first
        daemon = self.real_daemon(self.root, lambda: transport)
        errors = []
        def first():
            try: lsp.check_via_lsp(str(self.file), 'x', self.root, budget=CheckBudget(2, cancel.is_set))
            except CheckFailure as exc: errors.append(exc.kind)
        with patch.object(lsp, 'LeanDaemon', return_value=daemon):
            thread = threading.Thread(target=first)
            thread.start()
            self.assertTrue(entered.wait(1))
            try:
                result = lsp.check_via_lsp(str(self.file.with_name('Other.lean')), 'x', self.root, budget=CheckBudget(.5))
                self.assertIn('OK', result)
            finally:
                cancel.set(); thread.join(2)
        self.assertEqual(errors, ['cancelled'])

    def test_late_failure_cannot_reopen_a_recovered_outage(self):
        with patch.object(lsp, 'LeanDaemon', self.factory()):
            lsp.check_via_lsp(str(self.file), 'x', self.root)
            old = self.created[0]
            entered, release = threading.Event(), threading.Event()
            def old_check(path, content, *, budget=None):
                if path.endswith('Slow.lean'):
                    entered.set(); release.wait(2)
                raise CheckFailure('transport', 'old generation failed')
            old.check = old_check
            results = []
            slow = threading.Thread(target=lambda: results.append(lsp.check_via_lsp(
                str(self.file.with_name('Slow.lean')), 'x', self.root, budget=CheckBudget(3))))
            slow.start()
            self.assertTrue(entered.wait(1))
            try:
                result = lsp.check_via_lsp(str(self.file), 'x', self.root, budget=CheckBudget(2))
                self.assertIn('OK', result)
                self.assertEqual(lsp.runtime_snapshot(self.root)['state'], 'ready')
            finally:
                release.set(); slow.join(3)
            self.assertEqual(len(results), 1)
            self.assertEqual(len(self.created), 2)
            self.assertEqual(lsp.runtime_snapshot(self.root)['state'], 'ready')

    def test_concurrent_failure_shares_one_recovery_sequence(self):
        with patch.object(lsp, 'LeanDaemon', self.factory(99)):
            errors = []
            def work():
                try:
                    lsp.check_via_lsp(str(self.file), 'x', self.root, budget=CheckBudget(2))
                except CheckFailure as exc:
                    errors.append(exc.kind)
            threads = [threading.Thread(target=work) for _ in range(12)]
            for t in threads: t.start()
            for t in threads: t.join(3)
            self.assertEqual(len(errors), 12)
            self.assertEqual(len(self.created), 2)
            self.assertEqual(lsp._roots[self.root].failures, 1)

    def test_timeout_retires_daemon_without_fallback(self):
        transport = AutoTransport()
        original = transport.send
        def no_check_response(msg):
            if msg.get('method') != 'textDocument/waitForDiagnostics': original(msg)
        transport.send = no_check_response
        daemon = self.real_daemon(self.root, lambda: transport)
        with patch.object(lsp, 'LeanDaemon', return_value=daemon), patch('lea.lean_checks.subprocess.Popen') as popen:
            result = check_file(self.file, self.root, budget=CheckBudget(.03))
        self.assertEqual(result.execution['failure']['kind'], 'timeout')
        self.assertTrue(daemon.closed.wait(1))
        popen.assert_not_called()

    def test_cancel_request_closes_only_its_document(self):
        transport = AutoTransport()
        daemon = self.real_daemon(self.root, lambda: transport)
        self.assertTrue(daemon.start())
        daemon.check(str(self.file), 'x')
        stop = threading.Event()
        original = transport.send
        def cancel_on_wait(msg):
            if msg.get('method') == 'textDocument/waitForDiagnostics':
                stop.set()
            else: original(msg)
        transport.send = cancel_on_wait
        with self.assertRaises(CheckFailure) as error:
            daemon.check(str(self.file), 'x', budget=CheckBudget(1, stop.is_set))
        self.assertEqual(error.exception.kind, 'cancelled')
        self.assertFalse(daemon.broken)
        self.assertNotIn(self.file.as_uri(), daemon.opened)
        self.assertTrue(any(m.get('method') == '$/cancelRequest' for m in transport.sent))

    def test_cold_queue_timeout_does_not_spawn(self):
        lock = threading.Semaphore(0)
        with patch('lea.lean_checks._cold_check_sem', lock), patch('lea.lean_checks.subprocess.Popen') as popen:
            result = check_file(self.file, self.root, use_lsp=False, budget=CheckBudget(.02))
        self.assertEqual(result.execution['failure']['kind'], 'queue_timeout')
        popen.assert_not_called()

    def test_cancel_cold_process_reaps_it(self):
        real_popen = subprocess.Popen
        processes = []
        stop = threading.Event()
        def launch(*args, **kwargs):
            proc = real_popen([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
            processes.append(proc)
            stop.set()
            return proc
        with patch('lea.lean_checks.subprocess.Popen', launch):
            result = check_file(self.file, self.root, use_lsp=False, budget=CheckBudget(2, stop.is_set))
        self.assertEqual(result.execution['failure']['kind'], 'cancelled')
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_disabled_lsp_does_not_bypass_cascade_requirement(self):
        with patch.dict(os.environ, {'LEA_DISABLE_LSP': 'true'}), patch('lea.lean_checks.subprocess.Popen') as popen:
            result = check_file(self.file, self.root, allow_cold=False)
        self.assertEqual(result.execution['failure']['kind'], 'cascade_unavailable')
        popen.assert_not_called()

    def test_shutdown_includes_initializing_daemon(self):
        transport = AutoTransport()
        entered = threading.Event()
        original = transport.send
        def hanging_init(msg):
            if msg.get('method') == 'initialize': entered.set()
            else: original(msg)
        transport.send = hanging_init
        daemon = self.real_daemon(self.root, lambda: transport)
        errors = []
        def work():
            try: lsp.check_via_lsp(str(self.file), 'x', self.root, budget=CheckBudget(2))
            except CheckFailure as exc: errors.append(exc)
        with patch.object(lsp, 'LeanDaemon', return_value=daemon):
            thread = threading.Thread(target=work)
            thread.start()
            self.assertTrue(entered.wait(1))
            lsp._shutdown_all()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(transport.closed)
        self.assertNotIn(self.root, lsp._daemons)
        self.assertTrue(errors)

    def test_initialization_does_not_block_another_root(self):
        entered, release = threading.Event(), threading.Event()
        create = self.factory()
        def factory(root):
            d = create(root)
            original = d.start
            if root.endswith('slow'):
                def start(budget):
                    entered.set(); release.wait(2); return original(budget)
                d.start = start
            return d
        with patch.object(lsp, 'LeanDaemon', factory):
            slow = threading.Thread(target=lambda: lsp.check_via_lsp(str(self.file), 'x', self.root + '/slow', budget=CheckBudget(3)))
            slow.start()
            self.assertTrue(entered.wait(1))
            try:
                lsp.check_via_lsp(str(self.file), 'x', self.root, budget=CheckBudget(1))
            finally:
                release.set(); slow.join(3)

    def test_no_fallback_after_deadline_or_cancellation(self):
        for budget in (CheckBudget(0), CheckBudget(1, lambda: True)):
            with patch('lea.lean_checks.subprocess.Popen') as popen:
                result = check_file(self.file, self.root, budget=budget)
            self.assertEqual(result.status, 'error')
            popen.assert_not_called()

    def test_source_change_during_lsp_is_not_success(self):
        def changed(*args, **kwargs):
            self.file.write_text('bad revision')
            return 'OK — no errors, no warnings.'
        with patch.object(lsp, 'check_via_lsp', changed):
            result = check_file(self.file, self.root)
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.execution['failure']['kind'], 'source_changed')

    def test_cold_nonzero_empty_output(self):
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            return real_popen([sys.executable, '-c', 'raise SystemExit(7)'], **kwargs)
        with patch('lea.lean_checks.subprocess.Popen', launch):
            result = check_file(self.file, self.root, use_lsp=False)
        self.assertEqual(result.status, 'error')
        self.assertIn('code 7', result.output)
        self.assertEqual(result.execution['cold_reason'], 'explicit')
        self.assertEqual(result.execution['attempts'], 1)

    def test_cascade_does_not_use_unverified_cold_fallback(self):
        with patch.object(lsp, 'LeanDaemon', self.factory(2)), patch('lea.lean_checks.subprocess.Popen') as popen:
            result = check_file(self.file, self.root, allow_cold=False)
        self.assertEqual(result.execution['failure']['kind'], 'cascade_unavailable')
        popen.assert_not_called()

    def test_configuration_false_is_not_disabled(self):
        for value in ('0', 'false', ''):
            with patch.dict(os.environ, {'LEA_DISABLE_LSP': value}):
                self.assertFalse(lsp_disabled())

    def test_queue_wait_consumes_budget(self):
        lock = threading.Lock(); lock.acquire()
        with self.assertRaises(CheckFailure) as error:
            with CheckBudget(.02).acquire(lock): pass
        self.assertEqual(error.exception.kind, 'queue_timeout')
        lock.release()

    def test_diagnostics_can_arrive_before_tool_completion(self):
        token = diagnostics.begin_scope()
        try:
            import contextvars
            ctx = contextvars.copy_context()
            sent = threading.Event(); finish = threading.Event()
            def work():
                diagnostics.report('notice', 'lean.lsp_recovering', 'recovering', once=True, episode_id='one')
                sent.set(); finish.wait(1)
            t = threading.Thread(target=lambda: ctx.run(work)); t.start()
            self.assertTrue(sent.wait(1))
            self.assertEqual(len(diagnostics.drain()), 1)
            self.assertTrue(t.is_alive())
            finish.set(); t.join(1)
            diagnostics.report('notice', 'lean.lsp_recovering', 'recovering', once=True, episode_id='two')
            self.assertEqual(len(diagnostics.drain()), 1)
        finally: diagnostics.end_scope(token)


if __name__ == '__main__':
    unittest.main()
