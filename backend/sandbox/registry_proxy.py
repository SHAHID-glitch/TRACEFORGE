import os
import ipaddress
import select
import socket
import socketserver
from urllib.parse import urlsplit


def normalize_host(host: str) -> str:
    return host.rstrip(".").encode("idna").decode("ascii").lower()


def _public_addresses(host: str, port: int) -> list[tuple]:
    try:
        normalized = normalize_host(host)
        address = ipaddress.ip_address(normalized)
    except ValueError:
        address = None
    if address is not None:
        return []
    addresses = []
    for family, socktype, protocol, _, sockaddr in socket.getaddrinfo(
        normalized, port, type=socket.SOCK_STREAM
    ):
        resolved = ipaddress.ip_address(sockaddr[0])
        if resolved.version == 6 and resolved.ipv4_mapped is not None:
            resolved = resolved.ipv4_mapped
        if (
            resolved.is_private
            or resolved.is_loopback
            or resolved.is_link_local
            or resolved.is_multicast
            or resolved.is_unspecified
            or resolved.is_reserved
        ):
            continue
        addresses.append((family, socktype, protocol, sockaddr))
    return addresses


def allowed(host: str, port: int) -> bool:
    try:
        approved = {
            normalize_host(value)
            for value in os.environ.get(
                "TRACEFORGE_PROXY_HOSTS",
                os.environ.get("TRACEFORGE_PROXY_HOST", ""),
            ).split(",")
            if value.strip()
        }
        requested = normalize_host(host)
    except UnicodeError:
        return False
    return requested in approved and port in (443, 80) and bool(_public_addresses(requested, port))


def relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _, exceptional = select.select(sockets, [], sockets, 30)
        if exceptional:
            return
        if not readable:
            return
        for source in readable:
            target = right if source is left else left
            data = source.recv(65536)
            if not data:
                return
            target.sendall(data)


def _connect_validated(addresses: list[tuple]) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, protocol, sockaddr in addresses:
        upstream = socket.socket(family, socktype, protocol)
        upstream.settimeout(15)
        try:
            upstream.connect(sockaddr)
            return upstream
        except OSError as exc:
            last_error = exc
            upstream.close()
    raise last_error or OSError("No validated address could be connected.")


class ProxyHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(65536)
        if not line:
            return
        try:
            method, target, version = line.decode("latin1").strip().split(" ", 2)
            headers = {}
            while True:
                header = self.rfile.readline(65536)
                if header in (b"\r\n", b"\n", b""):
                    break
                name, value = header.decode("latin1").split(":", 1)
                headers[name.lower()] = value.strip()
            if method.upper() == "CONNECT":
                host, port_text = _connect_target(target)
                port = int(port_text or "443")
                addresses = _allowed_addresses(host, port)
                if not addresses:
                    self._forbidden()
                    return
                upstream = _connect_validated(addresses)
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                with upstream:
                    relay(self.connection, upstream)
                return
            parsed = urlsplit(target)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if method.upper() != "GET" or parsed.scheme not in {"http", "https"}:
                self._forbidden()
                return
            addresses = _allowed_addresses(host, port)
            if not addresses:
                self._forbidden()
                return
            upstream = _connect_validated(addresses)
            with upstream:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                upstream.sendall(f"{method} {path} {version}\r\n".encode("latin1"))
                for name, value in headers.items():
                    if name not in {"proxy-connection", "connection", "host"}:
                        upstream.sendall(f"{name}: {value}\r\n".encode("latin1"))
                upstream.sendall(f"Host: {normalize_host(host)}\r\nConnection: close\r\n\r\n".encode("latin1"))
                relay(self.connection, upstream)
        except (ValueError, UnicodeError, OSError):
            self._forbidden()

    def _forbidden(self) -> None:
        self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")


def _connect_target(target: str) -> tuple[str, str]:
    if target.startswith("["):
        closing = target.find("]")
        if closing < 0:
            raise ValueError("Malformed CONNECT target.")
        host = target[1:closing]
        remainder = target[closing + 1:]
        if remainder and not remainder.startswith(":"):
            raise ValueError("Malformed CONNECT target.")
        return host, remainder[1:]
    if target.count(":") > 1:
        raise ValueError("IPv6 CONNECT targets must be bracketed.")
    host, separator, port = target.partition(":")
    if not host or (separator and not port.isdigit()):
        raise ValueError("Malformed CONNECT target.")
    return host, port


def _allowed_addresses(host: str, port: int) -> list[tuple]:
    try:
        approved = {
            normalize_host(value)
            for value in os.environ.get(
                "TRACEFORGE_PROXY_HOSTS",
                os.environ.get("TRACEFORGE_PROXY_HOST", ""),
            ).split(",")
            if value.strip()
        }
        requested = normalize_host(host)
    except UnicodeError:
        return []
    if requested not in approved or port not in (443, 80):
        return []
    try:
        return _public_addresses(requested, port)
    except (OSError, ValueError):
        return []


class ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    with ThreadedServer(("0.0.0.0", 3128), ProxyHandler) as server:
        server.serve_forever()
