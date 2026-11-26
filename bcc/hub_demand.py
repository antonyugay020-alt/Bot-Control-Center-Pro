"""Opt-in local demand transport: only the elected Hub commits subscriptions.

The endpoint contains a workspace-local ephemeral challenge, not exchange
credentials. Demand acknowledgements never certify market observations.
"""
import json
import os
import queue
import secrets
import socket
import socketserver
import threading
import time
from concurrent.futures import Future, TimeoutError
from pathlib import Path

FLAG = 'BCC_PUBLIC_DEMAND_HUB_CANDIDATE'
LIMIT = 8192
BATCH = 32
CAPACITY = 64


def enabled():
    return os.environ.get(FLAG) == '1'


def endpoint_path(cache):
    return Path(str(Path(cache).resolve()) + '.demand.json')


def _receive(sock, deadline):
    data = bytearray()
    while len(data) < LIMIT:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Demand frame timed out')
        sock.settimeout(remaining)
        chunk = sock.recv(min(2048, LIMIT - len(data)))
        if not chunk:
            raise OSError('Incomplete demand frame')
        data.extend(chunk)
        if b'\n' in data:
            if data[-1:] != b'\n' or data.count(b'\n') != 1:
                raise ValueError('Expected exactly one demand frame')
            return json.loads(data)
    raise ValueError('Oversized demand frame')


def _send(sock, value, deadline):
    raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
    remaining = deadline - time.monotonic()
    if remaining <= 0 or len(raw) > LIMIT:
        raise OSError('Demand send limit exceeded')
    sock.settimeout(remaining)
    sock.sendall(raw)


class DemandClient:
    def __init__(self, cache):
        self.endpoint = endpoint_path(cache)

    def subscribe(self, url, params, *, lifetime=60, priority=False, force_due=False):
        from .market_hub import HubRegistry
        key = HubRegistry.identity(url, params)[0]
        try:
            endpoint = json.loads(self.endpoint.read_text(encoding='utf-8'))
            port, token = endpoint['port'], endpoint['token']
            if type(port) is not int or not 0 < port < 65536 or type(token) is not str or len(token) != 64:
                raise ValueError('Invalid demand endpoint')
            deadline = time.monotonic() + 2.
            with socket.create_connection(('127.0.0.1', port), timeout=1.) as sock:
                _send(sock, dict(token=token, request=dict(url=url, params=params,
                      lifetime=lifetime, priority=priority, force_due=force_due)), deadline)
                response = _receive(sock, deadline)
            if response != {'key': key}:
                raise OSError('Hub demand was not committed')
            return key
        except (ValueError, TypeError, KeyError, UnicodeError, OSError) as exc:
            raise OSError('Hub demand transport unavailable') from exc

    def request_once(self, url, params, *, lifetime=12, priority=False):
        return self.subscribe(url, params, lifetime=lifetime, priority=priority, force_due=True)


class _Listener(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = CAPACITY

    def __init__(self, server, handler):
        self.demand = server
        self.slots = threading.BoundedSemaphore(CAPACITY)
        super().__init__(('127.0.0.1', 0), handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.demand.count('overloaded')
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        hub = self.server.demand
        deadline = time.monotonic() + 1.8
        job = None
        try:
            frame = _receive(self.request, min(deadline, time.monotonic()+.5))
            if (type(frame) is not dict or set(frame) != {'token', 'request'}
                    or type(frame['token']) is not str or not frame['token'].isascii()
                    or not secrets.compare_digest(frame['token'], hub.token)
                    or type(frame['request']) is not dict
                    or set(frame['request']) != {'url','params','lifetime','priority','force_due'}):
                raise ValueError('Invalid demand request')
            prepared = hub.prepare(**frame['request'])
            job = Future()
            if hub.stop.is_set():
                raise OSError('Hub demand closing')
            hub.pending.put_nowait((job, prepared))
            hub.count('received')
            key = job.result(timeout=max(0., deadline-time.monotonic()))
            _send(self.request, {'key':key}, deadline)
        except Exception:
            if job is not None:
                job.cancel()
            hub.count('rejected')
            try:
                _send(self.request, {'error':'unavailable'}, deadline)
            except OSError:
                pass


class DemandServer:
    """One bounded batch consumer; networking threads never touch SQLite."""
    def __init__(self, cache, prepare, commit):
        self.endpoint = endpoint_path(cache)
        self.prepare, self.commit = prepare, commit
        self.token = secrets.token_hex(32)
        self.pending = queue.Queue(CAPACITY)
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._stats = dict(received=0, committed=0, batches=0, rejected=0,
                           overloaded=0, failed_batches=0, max_batch=0, error_types={})
        self.listener = _Listener(self, _Handler)
        self.receiver = threading.Thread(target=self.listener.serve_forever,
                                         kwargs={'poll_interval':.05}, name='hub-demand-receiver', daemon=True)
        self.writer = threading.Thread(target=self._run, name='hub-demand-batch', daemon=True)

    def count(self, name, n=1):
        with self._lock:
            self._stats[name] += n

    def stats(self):
        with self._lock:
            return dict(self._stats, error_types=dict(self._stats['error_types']), pending=self.pending.qsize())

    def start(self):
        from .storage import atomic_json
        self.writer.start()
        self.receiver.start()
        try:
            atomic_json(self.endpoint, dict(port=self.listener.server_address[1], token=self.token))
        except BaseException:
            self.close()
            raise

    def _run(self):
        while not self.stop.is_set():
            try:
                first = self.pending.get(timeout=.05)
            except queue.Empty:
                continue
            items = [first]
            # One short coalescing window, never wait for a full batch.
            until = time.monotonic()+.005
            while len(items) < BATCH and not self.stop.is_set():
                try:
                    items.append(self.pending.get(timeout=max(0., until-time.monotonic())))
                except queue.Empty:
                    break
            active = [(job, row) for job,row in items if job.set_running_or_notify_cancel()]
            if not active:
                continue
            try:
                if self.stop.is_set():
                    raise OSError('Hub demand closing')
                keys = self.commit([row for _,row in active])
                if len(keys) != len(active):
                    raise RuntimeError('Incomplete demand commit receipt')
            except Exception as exc:
                with self._lock:
                    self._stats['failed_batches'] += 1
                    name = type(exc).__name__
                    errors = self._stats['error_types']
                    errors[name] = errors.get(name,0)+1
                for job,_ in active:
                    job.set_exception(OSError('Hub demand commit failed'))
            else:
                with self._lock:
                    self._stats['batches'] += 1
                    self._stats['committed'] += len(keys)
                    self._stats['max_batch'] = max(self._stats['max_batch'],len(keys))
                for (job,_),key in zip(active,keys):
                    job.set_result(key)

    def close(self):
        self.stop.set()
        self.listener.shutdown()
        self.listener.server_close()
        self.receiver.join(2.)
        self.writer.join(3.)
        while True:
            try:
                job,_ = self.pending.get_nowait()
            except queue.Empty:
                break
            job.cancel()
        if self.writer.is_alive() or self.receiver.is_alive():
            raise RuntimeError('Hub demand shutdown did not complete')
        # Do not remove/overwrite a possible successor endpoint. A retired
        # endpoint cannot acknowledge new requests; the next leader replaces it.
