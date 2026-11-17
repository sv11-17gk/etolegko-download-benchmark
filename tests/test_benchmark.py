"""Тесты benchmark.py — целиком на локальном HTTP-сервере, без внешней сети."""

import argparse
import http.client
import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest import mock
from urllib.parse import urlsplit

import benchmark

IMAGE = bytes(range(256)) * 48  # 12 288 байт


class Handler(BaseHTTPRequestHandler):
    """Мини-стенд: файл, ошибки, переадресации, обрыв тела, «капельная» отдача."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass  # не засоряем вывод тестов

    def _send(self, status: int, body: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/image":
            self._send(200, IMAGE)
        elif path == "/notfound":
            self._send(404, b"nope")
        elif path == "/loop":
            self._send(302, b"", {"Location": "/loop"})
        elif path.startswith("/redirect/"):
            depth = int(path.removeprefix("/redirect/"))
            location = "/image" if depth <= 1 else f"/redirect/{depth - 1}"
            self._send(302, b"", {"Location": location})
        elif path == "/truncated":
            # Обещаем тело длиной len(IMAGE), отдаём кусок и закрываем соединение.
            self.send_response(200)
            self.send_header("Content-Length", str(len(IMAGE)))
            self.end_headers()
            self.wfile.write(IMAGE[:128])
            self.close_connection = True
        elif path == "/slow":
            # Отдаём тело по байту с паузой: каждая операция чтения клиента
            # укладывается в таймаут, но весь запрос — нет.
            self.send_response(200)
            self.send_header("Content-Length", "40")
            self.end_headers()
            for i in range(40):
                self.wfile.write(IMAGE[i : i + 1])
                self.wfile.flush()
                time.sleep(0.05)
        elif path == "/chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            for part in (b"hello ", b"world"):
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self._send(404, b"nope")


class QuietServer(ThreadingHTTPServer):
    """Глушит трассировки обрывов соединений — в тестах это штатные ситуации."""

    def handle_error(self, request: object, client_address: object) -> None:
        pass


class LocalServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = QuietServer(("127.0.0.1", 0), Handler)
        cls.worker = Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=5)

    def test_counts_final_body(self) -> None:
        result = benchmark.fetch_once(urlsplit(self.base + "/image"))
        self.assertEqual(result.size, len(IMAGE))
        self.assertEqual(result.status, 200)
        self.assertEqual(result.redirects, 0)
        self.assertGreater(result.elapsed, 0)

    def test_follows_redirects(self) -> None:
        result = benchmark.fetch_once(urlsplit(self.base + "/redirect/3"))
        self.assertEqual(result.redirects, 3)
        self.assertEqual(result.size, len(IMAGE))

    def test_redirect_loop_is_error(self) -> None:
        with self.assertRaises(http.client.HTTPException):
            benchmark.fetch_once(urlsplit(self.base + "/loop"))

    def test_http_error(self) -> None:
        with self.assertRaises(http.client.HTTPException) as context:
            benchmark.fetch_once(urlsplit(self.base + "/notfound"))
        self.assertIn("404", str(context.exception))

    def test_truncated_body_detected(self) -> None:
        with self.assertRaises(http.client.HTTPException):
            benchmark.fetch_once(urlsplit(self.base + "/truncated"))

    def test_chunked_body_counted(self) -> None:
        result = benchmark.fetch_once(urlsplit(self.base + "/chunked"))
        self.assertEqual(result.size, len(b"hello world"))

    def test_total_timeout(self) -> None:
        with self.assertRaises(TimeoutError):
            benchmark.fetch_once(urlsplit(self.base + "/slow"), timeout=1)

    def test_main_human_output(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = benchmark.main(["--count", "3", self.base + "/image"])
        self.assertEqual(code, 0)
        out = buffer.getvalue()
        self.assertIn("Скорость скачивания", out)
        self.assertIn("Среднее время запроса", out)
        self.assertIn(str(3 * len(IMAGE)), out.replace(" ", ""))

    def test_main_json_output(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = benchmark.main(["--json", "--count", "2", self.base + "/image"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["total_bytes"], 2 * len(IMAGE))
        self.assertEqual(payload["requests_done"], 2)
        self.assertGreater(payload["speed_mib_per_second"], 0)

    def test_main_failure_returns_one(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = benchmark.main([self.base + "/notfound"])
        self.assertEqual(code, 1)
        self.assertIn("Запрос 1/10", err.getvalue())

    def test_main_interrupt_returns_130(self) -> None:
        with mock.patch.object(benchmark, "fetch_once", side_effect=KeyboardInterrupt):
            code = benchmark.main([self.base + "/image"])
        self.assertEqual(code, 130)


class UnitsTest(unittest.TestCase):
    def test_http_url_accepts_valid(self) -> None:
        url = benchmark.http_url("https://example.com/a?b=1#frag")
        self.assertEqual(url.hostname, "example.com")
        self.assertEqual(url.query, "b=1")

    def test_http_url_rejects_invalid(self) -> None:
        for value in (
            "ftp://example.com/",
            "example.com",
            "http://",
            "http://user:pass@example.com/",
            "http://example.com:0/",
            "http://example.com:99999/",
        ):
            with self.assertRaises(argparse.ArgumentTypeError, msg=value):
                benchmark.http_url(value)

    def test_absolute_keeps_query(self) -> None:
        # Регрессия: query не должен попадать во фрагмент при сборке URL обратно.
        url = benchmark.http_url("https://example.com/p?bytes=7")
        self.assertEqual(benchmark._absolute(url), "https://example.com/p?bytes=7")

    def test_human_bytes(self) -> None:
        self.assertEqual(benchmark.human_bytes(0), "0 Б")
        self.assertEqual(benchmark.human_bytes(2048), "2.0 КиБ")
        self.assertEqual(benchmark.human_bytes(3 * benchmark.MIB), "3.0 МиБ")


if __name__ == "__main__":
    unittest.main()
