"""Measure ten sequential HTTP downloads without storing their bodies."""

import argparse
import http.client
import sys
from time import perf_counter
from urllib.parse import urlsplit

REQUEST_COUNT = 10
READ_SIZE = 64 * 1024
SOCKET_TIMEOUT = 30
MIB = 1024 * 1024


def http_url(value):
    try:
        url = urlsplit(value)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.port == 0
        ):
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError(
            "укажите корректный HTTP(S)-адрес без логина и пароля"
        ) from None
    return url


def download(url):
    connection_type = (
        http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(url.hostname, url.port, timeout=SOCKET_TIMEOUT)
    target = url.path or "/"
    if url.query:
        target += "?" + url.query

    size = 0
    started = perf_counter()
    try:
        connection.request("GET", target, headers={"Accept-Encoding": "identity"})
        with connection.getresponse() as response:
            if not 200 <= response.status < 300 or response.status == 206:
                raise http.client.HTTPException(f"HTTP {response.status} {response.reason}")

            expected_size = response.length
            while chunk := response.read(READ_SIZE):
                size += len(chunk)

            # Bounded reads can return EOF quietly after an interrupted fixed-length body.
            if expected_size is not None and size != expected_size:
                raise http.client.IncompleteRead(b"", expected_size - size)

            elapsed = perf_counter() - started
    finally:
        connection.close()
    return size, elapsed


def main():
    parser = argparse.ArgumentParser(description="Измеряет 10 последовательных HTTP-загрузок.")
    parser.add_argument("url", metavar="URL", type=http_url, help="прямой адрес ресурса")
    args = parser.parse_args()

    total_bytes = 0
    total_seconds = 0.0
    for attempt in range(1, REQUEST_COUNT + 1):
        try:
            size, elapsed = download(args.url)
        except (OSError, http.client.HTTPException, ValueError) as error:
            print(f"Запрос {attempt}/{REQUEST_COUNT}: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nИзмерение прервано.", file=sys.stderr)
            return 130
        total_bytes += size
        total_seconds += elapsed

    print(f"Запросов: {REQUEST_COUNT}")
    print(f"Среднее время запроса: {total_seconds / REQUEST_COUNT:.3f} с")
    print(f"Скачано: {total_bytes} байт")
    print(f"Скорость скачивания: {total_bytes / MIB / total_seconds:.3f} MiB/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
