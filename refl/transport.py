"""A small JSON-over-HTTP client for llama-server, built on the standard library."""
import asyncio
import http.client
import json
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass


class TransportError(Exception):
    """A request that did not get an HTTP response: no connection, a timeout, or a broken reply."""


class ReplyTimeout(TransportError):
    """A server that accepted the request but sent no complete reply in time."""

    def __init__(self, seconds):
        super().__init__(f"no reply within {seconds} s")
        self.seconds = seconds


@dataclass
class Response:
    status: int
    reason: str
    body: bytes
    seconds: float

    @property
    def is_success(self):
        return 200 <= self.status < 300

    def json(self):
        """Return the decoded JSON body. Raise ValueError when the body is not JSON."""
        return json.loads(self.body.decode("utf-8"))


class _Call:
    """One connection, which another thread can abort while a request waits on it."""

    def __init__(self, connection):
        self.connection = connection
        self.aborted = False

    def abort(self):
        """Make the worker thread's connect, send, or read fail now. The worker closes the connection."""
        self.aborted = True
        sock = self.connection.sock
        if sock is None:
            return
        # shutdown wakes a read that is blocked on Linux and macOS, but not on Windows.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        # The response reads through a file object, which keeps the socket open after sock.close(). Closing the
        # detached descriptor itself wakes a blocked read on Windows.
        try:
            socket.close(sock.detach())
        except OSError:
            pass


class HttpClient:
    """Send JSON requests to one server. Each request opens its own connection in a worker thread."""

    def __init__(self, base_url, *, headers=None, timeout=180, connect_timeout=5, on_response=None):
        parts = urllib.parse.urlsplit(base_url)
        try:
            port = parts.port
        except ValueError:
            port = -1
        if parts.scheme not in ("http", "https") or not parts.hostname or port == -1 or parts.query:
            raise ValueError(f"Invalid llama-server URL: {base_url}. Use a form such as http://localhost:8080.")
        self.base_url = base_url
        self.https = parts.scheme == "https"
        self.host = parts.hostname
        self.port = port
        self.prefix = parts.path.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        # Called as on_response(method, path, payload, response) after every response, for measurements.
        self.on_response = on_response
        self.ssl_context = ssl.create_default_context() if self.https else None
        # The address that accepted the last connection, tried first next time.
        self.last_address = None

    async def request(self, method, path, *, json=None, params=None, timeout=None):
        target = self.prefix + path
        if params:
            target += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", **self.headers}
        body = None
        if json is not None:
            body = _encode(json)
            headers["Content-Type"] = "application/json"
        timeout = self.timeout if timeout is None else timeout
        call = _Call(self._connection())
        try:
            response = await asyncio.to_thread(self._send, call, method, target, body, headers, timeout,
                                               min(self.connect_timeout, timeout))
        except asyncio.CancelledError:
            call.abort()
            raise
        if self.on_response is not None:
            self.on_response(method, path, json, response)
        return response

    def _connection(self):
        # The connection gets its socket from _open_socket. It still names the host in the Host header.
        if self.https:
            return http.client.HTTPSConnection(self.host, self.port, context=self.ssl_context)
        return http.client.HTTPConnection(self.host, self.port)

    def _addresses(self):
        """Resolve the host with IPv4 first and the address that connected last at the front."""
        port = self.port or (443 if self.https else 80)
        infos = socket.getaddrinfo(self.host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys((family, address) for family, _, _, _, address in infos))
        # Windows lists ::1 before 127.0.0.1 for localhost and takes two seconds to give up on an address where
        # nothing listens. llama-server listens on 127.0.0.1 by default.
        return sorted(addresses, key=lambda item: (item != self.last_address, item[0] != socket.AF_INET))

    def _open_socket(self, timeout):
        errors = []
        for family, address in self._addresses():
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                sock.connect(address)
            except OSError as error:
                sock.close()
                errors.append(error)
                continue
            self.last_address = (family, address)
            return sock
        raise errors[-1] if errors else OSError(f"{self.host} has no address")

    def _send(self, call, method, target, body, headers, timeout, connect_timeout):
        started = time.monotonic()
        connection = call.connection
        try:
            sock = self._open_socket(connect_timeout)
            if self.https:
                sock = self.ssl_context.wrap_socket(sock, server_hostname=self.host)
            connection.sock = sock
            if call.aborted:
                raise TransportError("the request was cancelled")
            # The connect timeout ends here. Reading the reply may take as long as the model needs.
            sock.settimeout(timeout)
            connection.request(method, target, body=body, headers=headers)
            reply = connection.getresponse()
            data = reply.read()
            return Response(reply.status, reply.reason, data, time.monotonic() - started)
        except socket.timeout as error:
            if connection.sock:
                raise ReplyTimeout(timeout) from error
            raise TransportError(f"no connection within {connect_timeout} s") from error
        except (OSError, http.client.HTTPException) as error:
            raise TransportError(str(error) or type(error).__name__) from error
        finally:
            connection.close()


def _encode(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")
