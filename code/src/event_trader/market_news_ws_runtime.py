"""Soft-fail realtime market-news WebSocket runtime."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlencode, urlparse, urlunparse

from event_trader.contracts._validators import validate_target_key
from event_trader.feeds.benzinga_news_ws import (
    BenzingaNewsWebSocketEvent,
    parse_benzinga_news_ws_message,
)
from event_trader.feeds.market_news import (
    MarketNewsArticle,
    market_news_article_to_live_input,
)
from event_trader.market_news_schedule import MarketNewsWebSocketSessionPolicy
from event_trader.market_news_ws_status import MarketNewsWebSocketStatusWriter
from event_trader.runtime.source_publisher import RuntimeRawSourcePublisher
from event_trader.source_archive.market_news import (
    append_market_news_record,
    map_live_market_news_to_archive_record,
    market_news_record_id,
)
from event_trader.source_release import release_current_market_news_record_ids
from event_trader.storage import WorkspaceLayout


class MarketNewsWebSocketRuntimeError(ValueError):
    """Raised when the market-news WebSocket runtime receives invalid state."""


type MarketNewsWebSocketEmitter = Callable[[str], None]
type MessageSourceFactory = Callable[[], Iterable[str]]

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

@dataclass(frozen=True, slots=True)
class LiveMarketNewsWebSocketReceipt:
    """Observable result for one processed WebSocket event."""

    target_key: str
    action: str
    source_ref: str
    released_count: int


@dataclass(frozen=True, slots=True)
class MarketNewsWebSocketRoute:
    """One target route for a shared market-news WebSocket connection."""

    target_key: str
    symbols: tuple[str, ...]
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_key",
            validate_target_key(
                self.target_key,
                error_type=MarketNewsWebSocketRuntimeError,
            ),
        )
        object.__setattr__(self, "symbols", _validate_symbols(self.symbols))
        object.__setattr__(self, "labels", tuple(self.labels))


@dataclass(slots=True)
class MarketNewsWebSocketDriver:
    """Background soft-fail Benzinga news WebSocket driver."""

    provider: str
    url: str
    token: str
    tickers: tuple[str, ...]
    routes: tuple[MarketNewsWebSocketRoute, ...]
    soft_fail: bool
    reconnect_seconds_min: int
    reconnect_seconds_max: int
    heartbeat_seconds: int
    session_policy: MarketNewsWebSocketSessionPolicy
    raw_source_publisher: RuntimeRawSourcePublisher
    historical_layout: WorkspaceLayout
    status_writer: MarketNewsWebSocketStatusWriter | None = None
    emit: MarketNewsWebSocketEmitter | None = None
    message_source_factory: MessageSourceFactory | None = None
    _stop_event: threading.Event = field(init=False, repr=False)
    _thread: threading.Thread | None = field(init=False, default=None, repr=False)
    _connection: _WebSocketConnection | None = field(init=False, default=None, repr=False)
    _run_error: BaseException | None = field(init=False, default=None, repr=False)
    _reconnect_seconds: int = field(init=False, repr=False)
    _start_blocked: bool = field(init=False, default=False, repr=False)
    _status_error_emitted: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        self.provider = _validate_non_blank(self.provider, field_name="provider")
        self.url = _validate_non_blank(self.url, field_name="url")
        self.token = "" if self.token is None else str(self.token).strip()
        self.tickers = _validate_symbols(self.tickers)
        self.routes = _validate_routes(self.routes)
        self.reconnect_seconds_min = _validate_positive_int(
            self.reconnect_seconds_min,
            field_name="reconnect_seconds_min",
        )
        self.reconnect_seconds_max = _validate_positive_int(
            self.reconnect_seconds_max,
            field_name="reconnect_seconds_max",
        )
        if self.reconnect_seconds_min > self.reconnect_seconds_max:
            raise MarketNewsWebSocketRuntimeError(
                "reconnect_seconds_min must be less than or equal to reconnect_seconds_max."
            )
        self.heartbeat_seconds = _validate_positive_int(
            self.heartbeat_seconds,
            field_name="heartbeat_seconds",
        )
        if not isinstance(self.session_policy, MarketNewsWebSocketSessionPolicy):
            raise MarketNewsWebSocketRuntimeError(
                "session_policy must be a MarketNewsWebSocketSessionPolicy instance."
            )
        if not isinstance(self.raw_source_publisher, RuntimeRawSourcePublisher):
            raise MarketNewsWebSocketRuntimeError(
                "raw_source_publisher must be a RuntimeRawSourcePublisher instance."
            )
        if not isinstance(self.historical_layout, WorkspaceLayout):
            raise MarketNewsWebSocketRuntimeError(
                "historical_layout must be a WorkspaceLayout instance."
            )
        if self.status_writer is not None and not isinstance(
            self.status_writer,
            MarketNewsWebSocketStatusWriter,
        ):
            raise MarketNewsWebSocketRuntimeError(
                "status_writer must be a MarketNewsWebSocketStatusWriter instance."
            )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connection: _WebSocketConnection | None = None
        self._run_error: BaseException | None = None
        self._reconnect_seconds = self.reconnect_seconds_min
        self._start_blocked = False
        self._status_error_emitted = False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def start_blocked(self) -> bool:
        return self._start_blocked

    def record_session_state(self, *, current_time: datetime, session_active: bool) -> None:
        self._record_status(
            lambda writer: writer.record_session_state(
                current_time=current_time,
                session_active=session_active,
                running=self.is_running,
                start_blocked=self.start_blocked,
            )
        )

    def start(self) -> None:
        if self.is_running:
            raise MarketNewsWebSocketRuntimeError("market-news websocket already started.")
        if self._thread is not None:
            self.join(timeout_seconds=0)
        current_time = datetime.now(UTC)
        self._record_status(
            lambda writer: writer.record_start_attempt(current_time=current_time)
        )
        if not self.token:
            message = (
                "warning: market_news websocket disabled: "
                f"provider={self.provider} missing_token=true"
            )
            _emit(self.emit, message)
            if self.soft_fail:
                self._start_blocked = True
                self._record_status(
                    lambda writer: writer.record_start_blocked(
                        current_time=current_time,
                        reason="missing_token",
                    )
                )
                return
            raise MarketNewsWebSocketRuntimeError(message)
        self._stop_event = threading.Event()
        self._connection = None
        self._run_error = None
        self._reconnect_seconds = self.reconnect_seconds_min
        self._start_blocked = False
        self._thread = threading.Thread(
            target=self._run_forever,
            name="market-news-ws",
            daemon=True,
        )
        self._thread.start()
        self._record_status(lambda writer: writer.record_started(current_time=current_time))

    def stop(self) -> None:
        self._stop_event.set()
        connection = self._connection
        if connection is not None:
            connection.close()
        self._record_status(
            lambda writer: writer.record_stopped(
                current_time=datetime.now(UTC),
                reason="stop_requested",
            )
        )

    def join(self, *, timeout_seconds: float | None = None) -> None:
        if self._thread is None:
            return
        self._thread.join(timeout=timeout_seconds)
        if self._run_error is not None and not self.soft_fail:
            raise MarketNewsWebSocketRuntimeError(
                f"market-news websocket failed: {self._run_error}"
            ) from self._run_error

    def _run_forever(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    self._run_once()
                    self._reconnect_seconds = self.reconnect_seconds_min
                    if self.message_source_factory is not None:
                        break
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    error_summary = _exception_summary(exc)
                    _emit(
                        self.emit,
                        "warning: market_news websocket degraded: "
                        f"provider={self.provider} "
                        f"error={error_summary}",
                    )
                    self._record_error_status(error_summary=error_summary)
                    if not self.soft_fail:
                        raise
                    self._sleep_before_reconnect()
        except BaseException as exc:
            self._run_error = exc
            if self.soft_fail:
                error_summary = _exception_summary(exc)
                _emit(
                    self.emit,
                    "warning: market_news websocket stopped: "
                    f"provider={self.provider} "
                    f"error={error_summary}",
                )
                self._record_status(
                    lambda writer: writer.record_error(
                        current_time=datetime.now(UTC),
                        error=error_summary,
                    )
                )

    def _run_once(self) -> None:
        source = (
            self.message_source_factory()
            if self.message_source_factory is not None
            else self._connect_default_source()
        )
        for message in source:
            if self._stop_event.is_set():
                break
            self._process_message(message, captured_at=datetime.now(UTC))

    def _connect_default_source(self) -> _WebSocketMessageSource:
        stream_url = _benzinga_stream_url(
            url=self.url,
            token=self.token,
            tickers=self.tickers,
        )
        connection = _WebSocketConnection.connect(
            stream_url,
            timeout_seconds=self.heartbeat_seconds,
        )
        self._connection = connection
        _emit(
            self.emit,
            "live market_news websocket connected: "
            f"provider={self.provider} tickers={','.join(self.tickers)}",
        )
        self._record_status(
            lambda writer: writer.record_connected(current_time=datetime.now(UTC))
        )
        return _WebSocketMessageSource(
            connection=connection,
            on_close=self._clear_connection,
            on_pong=self._record_pong,
        )

    def _clear_connection(self) -> None:
        self._connection = None

    def _record_pong(self) -> None:
        self._record_status(lambda writer: writer.record_pong(current_time=datetime.now(UTC)))

    def _process_message(self, message: str, *, captured_at: datetime) -> None:
        self._record_status(lambda writer: writer.record_message(current_time=captured_at))
        events = parse_benzinga_news_ws_message(message)
        self._record_status(
            lambda writer: writer.record_parsed_events(
                current_time=captured_at,
                event_count=len(events),
            )
        )
        for event in events:
            if event.action == "deleted":
                _emit(
                    self.emit,
                    "warning: market_news websocket deletion observed: "
                    f"provider={self.provider}",
                )
                continue
            article = event.article
            if article is None:
                continue
            matching_routes = _matching_routes(article, self.routes)
            if not matching_routes:
                continue
            self._record_status(
                lambda writer: writer.record_matched_event(current_time=captured_at)
            )
            for route in matching_routes:
                self._release_article(
                    event=event,
                    article=article,
                    route=route,
                    captured_at=captured_at,
                )

    def _release_article(
        self,
        *,
        event: BenzingaNewsWebSocketEvent,
        article: MarketNewsArticle,
        route: MarketNewsWebSocketRoute,
        captured_at: datetime,
    ) -> LiveMarketNewsWebSocketReceipt:
        ingress_input = market_news_article_to_live_input(
            article,
            captured_at=captured_at,
        )
        archived_record = map_live_market_news_to_archive_record(
            target_key=route.target_key,
            article=article,
            ingress_input=ingress_input,
            labels=list(route.labels),
            provider=self.provider,
        )
        archive_receipt = append_market_news_record(
            self.historical_layout,
            record=archived_record,
        )
        record_id = market_news_record_id(archived_record)
        releaseable = (
            archive_receipt.status == "written"
            and record_id
            in release_current_market_news_record_ids(
                layout=self.historical_layout,
                target_key=route.target_key,
                current_records=(archived_record,),
            )
        )
        self._record_status(lambda writer: writer.record_archived(current_time=captured_at))
        if not releaseable:
            return LiveMarketNewsWebSocketReceipt(
                target_key=route.target_key,
                action=event.action,
                source_ref=article.source_ref,
                released_count=0,
            )
        raw_receipt = self.raw_source_publisher.publish_news_raw(
            target_key=route.target_key,
            source_ref=ingress_input.source_ref,
            record_id=record_id,
            event_time=archived_record.visible_at,
            recorded_at=captured_at,
        )
        self._record_status(
            lambda writer: writer.record_released(
                current_time=captured_at,
                released_count=1,
            )
        )
        _emit(
            self.emit,
            "live market_news websocket event: "
            f"target_key={route.target_key} action={event.action} "
            f"source_ref={article.source_ref!r} raw_record_id={raw_receipt.record_id}",
        )
        return LiveMarketNewsWebSocketReceipt(
            target_key=route.target_key,
            action=event.action,
            source_ref=article.source_ref,
            released_count=1,
        )

    def _record_status(
        self,
        update: Callable[[MarketNewsWebSocketStatusWriter], None],
    ) -> None:
        writer = self.status_writer
        if writer is None:
            return
        try:
            update(writer)
        except Exception as exc:
            if self._status_error_emitted:
                return
            self._status_error_emitted = True
            _emit(
                self.emit,
                "warning: market_news websocket status degraded: "
                f"provider={self.provider} error={_exception_summary(exc)}",
            )

    def _record_error_status(self, *, error_summary: str) -> None:
        self._record_status(
            lambda writer: writer.record_error(
                current_time=datetime.now(UTC),
                error=error_summary,
            )
        )

    def _sleep_before_reconnect(self) -> None:
        deadline = time.monotonic() + self._reconnect_seconds
        self._reconnect_seconds = min(
            self.reconnect_seconds_max,
            max(self.reconnect_seconds_min, self._reconnect_seconds * 2),
        )
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            time.sleep(min(0.2, deadline - time.monotonic()))


class _WebSocketMessageSource:
    def __init__(
        self,
        *,
        connection: _WebSocketConnection,
        on_close: Callable[[], None],
        on_pong: Callable[[], None],
    ) -> None:
        self.connection = connection
        self.on_close = on_close
        self.on_pong = on_pong

    def __iter__(self) -> _WebSocketMessageSource:
        return self

    def __next__(self) -> str:
        while True:
            try:
                message = self.connection.recv_text()
            except TimeoutError:
                try:
                    self.connection.send_text("ping")
                except Exception:
                    self.connection.close()
                    self.on_close()
                    raise
                continue
            except Exception:
                self.connection.close()
                self.on_close()
                raise
            if message.strip().lower() == "pong":
                self.on_pong()
                continue
            return message


class _WebSocketConnection:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    @classmethod
    def connect(cls, url: str, *, timeout_seconds: int) -> _WebSocketConnection:
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise MarketNewsWebSocketRuntimeError("websocket URL must be ws:// or wss://.")
        if not parsed.hostname:
            raise MarketNewsWebSocketRuntimeError("websocket URL must include a host.")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        raw_sock = socket.create_connection(
            (parsed.hostname, port),
            timeout=float(timeout_seconds),
        )
        sock = (
            ssl.create_default_context().wrap_socket(
                raw_sock,
                server_hostname=parsed.hostname,
            )
            if parsed.scheme == "wss"
            else raw_sock
        )
        sock.settimeout(float(timeout_seconds))
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = cls._read_http_response(sock)
        accept = base64.b64encode(
            hashlib.sha1(f"{key}{_WEBSOCKET_GUID}".encode("ascii")).digest()
        ).decode("ascii")
        response_lower = response.lower()
        if not response.startswith("HTTP/1.1 101") and not response.startswith("HTTP/1.0 101"):
            sock.close()
            raise MarketNewsWebSocketRuntimeError(
                f"websocket handshake was not accepted: {_truncate(response)}"
            )
        if f"sec-websocket-accept: {accept.lower()}" not in response_lower:
            sock.close()
            raise MarketNewsWebSocketRuntimeError("websocket accept header mismatch.")
        return cls(sock)

    def recv_text(self) -> str:
        while True:
            try:
                opcode, payload = self._read_frame()
            except TimeoutError as exc:
                raise TimeoutError("websocket idle timeout.") from exc
            if opcode == 0x1:
                return payload.decode("utf-8")
            if opcode == 0x8:
                raise MarketNewsWebSocketRuntimeError("websocket closed by server.")
            if opcode == 0x9:
                self._sock.sendall(_encode_client_frame(payload, opcode=0xA))

    def send_text(self, message: str) -> None:
        self._sock.sendall(_encode_client_frame(message.encode("utf-8"), opcode=0x1))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            return

    def _read_frame(self) -> tuple[int, bytes]:
        header = self._recv_exact(2)
        first, second = header[0], header[1]
        fin = (first & 0x80) != 0
        opcode = first & 0x0F
        masked = (second & 0x80) != 0
        length = second & 0x7F
        if not fin:
            raise MarketNewsWebSocketRuntimeError("fragmented websocket frames are unsupported.")
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _recv_exact(self, length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = length
        while remaining:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise MarketNewsWebSocketRuntimeError("websocket connection closed.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _read_http_response(sock: socket.socket) -> str:
        chunks: list[bytes] = []
        while b"\r\n\r\n" not in b"".join(chunks):
            chunk = sock.recv(4096)
            if not chunk:
                raise MarketNewsWebSocketRuntimeError("websocket handshake closed early.")
            chunks.append(chunk)
        return b"".join(chunks).decode("iso-8859-1")


def _benzinga_stream_url(*, url: str, token: str, tickers: tuple[str, ...]) -> str:
    parsed = urlparse(url)
    query = urlencode({"token": token, "tickers": ",".join(tickers)})
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            parsed.params,
            query,
            parsed.fragment,
        )
    )


def _encode_client_frame(payload: bytes, *, opcode: int) -> bytes:
    first = 0x80 | opcode
    mask = os.urandom(4)
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", first, 0x80 | length)
    elif length < 65536:
        header = struct.pack("!BBH", first, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", first, 0x80 | 127, length)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return header + mask + masked


def _matching_routes(
    article: MarketNewsArticle,
    routes: tuple[MarketNewsWebSocketRoute, ...],
) -> tuple[MarketNewsWebSocketRoute, ...]:
    article_symbols = {symbol.upper() for symbol in article.symbols}
    return tuple(
        route for route in routes if article_symbols & set(route.symbols)
    )


def _validate_routes(
    routes: tuple[MarketNewsWebSocketRoute, ...],
) -> tuple[MarketNewsWebSocketRoute, ...]:
    if not isinstance(routes, tuple) or not routes:
        raise MarketNewsWebSocketRuntimeError("routes must be a non-empty tuple.")
    validated: list[MarketNewsWebSocketRoute] = []
    seen: set[str] = set()
    for route in routes:
        if not isinstance(route, MarketNewsWebSocketRoute):
            raise MarketNewsWebSocketRuntimeError(
                "routes must contain only MarketNewsWebSocketRoute instances."
            )
        if route.target_key in seen:
            raise MarketNewsWebSocketRuntimeError("routes must not contain duplicate target_key.")
        seen.add(route.target_key)
        validated.append(route)
    return tuple(validated)


def _validate_symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(symbols, tuple) or not symbols:
        raise MarketNewsWebSocketRuntimeError("tickers must be a non-empty tuple.")
    normalized: list[str] = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not symbol.strip():
            raise MarketNewsWebSocketRuntimeError("tickers must contain only non-empty strings.")
        value = symbol.strip().upper()
        if value in normalized:
            raise MarketNewsWebSocketRuntimeError("tickers must not contain duplicates.")
        normalized.append(value)
    return tuple(normalized)


def _validate_non_blank(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarketNewsWebSocketRuntimeError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _validate_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketNewsWebSocketRuntimeError(f"{field_name} must be a positive integer.")
    if value <= 0:
        raise MarketNewsWebSocketRuntimeError(f"{field_name} must be greater than zero.")
    return value


def _emit(emit: MarketNewsWebSocketEmitter | None, message: str) -> None:
    if emit is not None:
        emit(message)


def _exception_summary(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return _truncate(text)


def _truncate(value: str, *, limit: int = 240) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


__all__ = [
    "LiveMarketNewsWebSocketReceipt",
    "MarketNewsWebSocketDriver",
    "MarketNewsWebSocketRoute",
    "MarketNewsWebSocketRuntimeError",
]
