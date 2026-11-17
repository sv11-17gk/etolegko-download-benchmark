"""Измерение скорости HTTP-загрузки: серия последовательных GET-запросов.

Скрипт делает N последовательных GET-запросов (по умолчанию 10) к указанному
HTTP(S)-адресу, дожидается полного ответа на каждый и выводит среднее время
запроса, суммарный объём полученных данных и скорость скачивания в MiB/s.

Только стандартная библиотека (Python 3.10+). Тело ответа читается блоками
и отбрасывается: файл не попадает ни на диск, ни целиком в память. Каждый
запрос идёт по новому соединению, поэтому в замер входят DNS, TCP и TLS.
Переадресации следуются автоматически (до 5); обрыв тела при известном
Content-Length считается ошибкой, а не успешным запросом.
"""

import argparse
import http.client
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

__version__ = "1.0.0"

DEFAULT_COUNT = 10
DEFAULT_TIMEOUT = 30  # секунд — лимит на один запрос целиком
READ_SIZE = 64 * 1024
MAX_REDIRECTS = 5
MIB = 1024 * 1024

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
REQUEST_HEADERS = {
    "User-Agent": f"http-benchmark/{__version__}",
    # Просим сервер не сжимать тело: измеряем байты самого ресурса.
    "Accept-Encoding": "identity",
}


@dataclass(frozen=True)
class FetchResult:
    """Итог одного запроса: объём тела финального ответа и затраченное время."""

    size: int
    elapsed: float
    status: int
    redirects: int


def http_url(value: str) -> SplitResult:
    """argparse-тип: принимает только http(s)-URL без учётных данных."""
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


def bounded_int(name: str, low: int, high: int) -> Callable[[str], int]:
    """Фабрика argparse-типов: целое число в границах [low, high]."""

    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name}: ожидается целое число") from None
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"{name}: допустимо от {low} до {high}")
        return number

    return parse


def human_bytes(size: int) -> str:
    """Человекочитаемый объём в двоичных единицах: 2048 -> '2.0 КиБ'."""
    value = float(size)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if value < 1024 or unit == "ТиБ":
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024


def _target(url: SplitResult) -> str:
    """Адрес запроса: путь + query-строка; отсутствующий путь заменяется на '/'."""
    target = url.path or "/"
    if url.query:
        target += "?" + url.query
    return target


def _absolute(url: SplitResult) -> str:
    """URL строкой без фрагмента — база для urljoin и для вывода."""
    return urlunsplit((url.scheme, url.netloc, url.path or "/", url.query, ""))


def _open(url: SplitResult, timeout: float) -> http.client.HTTPConnection:
    """Новое соединение к хосту: без переиспользования между запросами."""
    factory = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    return factory(url.hostname, url.port, timeout=timeout)


def _follow_redirect(response: http.client.HTTPResponse, base: SplitResult) -> SplitResult:
    """Адрес из заголовка Location; относительные пути разрешаются от текущего URL."""
    location = response.getheader("Location")
    if not location:
        raise http.client.HTTPException(f"HTTP {response.status} без заголовка Location")
    url = urlsplit(urljoin(_absolute(base), location))
    if url.scheme not in ("http", "https") or not url.hostname:
        raise http.client.HTTPException(f"переадресация на неподдерживаемый адрес: {location}")
    return url


def fetch_once(url: SplitResult, timeout: float = DEFAULT_TIMEOUT) -> FetchResult:
    """Один запрос с ожиданием полного тела ответа.

    Время измеряется от отправки первого GET до последнего байта финального
    ответа; переадресации входят в замер. Таймаут действует на операции сокета,
    а суммарное время дополнительно проверяется после чтения блока тела.
    """
    started = perf_counter()
    deadline = started + timeout
    current = url
    redirects = 0
    while True:
        connection = _open(current, timeout)
        try:
            connection.request("GET", _target(current), headers=REQUEST_HEADERS)
            response = connection.getresponse()

            if response.status in REDIRECT_STATUSES:
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise http.client.HTTPException(f"более {MAX_REDIRECTS} переадресаций подряд")
                current = _follow_redirect(response, current)
                continue

            if not 200 <= response.status < 300:
                raise http.client.HTTPException(f"HTTP {response.status} {response.reason}")

            expected = response.length
            size = 0
            while chunk := response.read(READ_SIZE):
                size += len(chunk)
                if perf_counter() > deadline:
                    raise TimeoutError(f"запрос не уложился в {timeout:g} с")

            # read(N) может тихо вернуть EOF раньше обещанного Content-Length — проверяем.
            if expected is not None and size != expected:
                raise http.client.IncompleteRead(b"", expected - size)

            return FetchResult(size, perf_counter() - started, response.status, redirects)
        finally:
            connection.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Измеряет скорость серии последовательных HTTP-загрузок.",
    )
    parser.add_argument(
        "url", metavar="URL", type=http_url, help="прямой адрес ресурса (http или https)"
    )
    parser.add_argument(
        "--count",
        type=bounded_int("--count", 1, 1000),
        default=DEFAULT_COUNT,
        metavar="N",
        help=f"число запросов (по умолчанию {DEFAULT_COUNT} — по условию задачи)",
    )
    parser.add_argument(
        "--timeout",
        type=bounded_int("--timeout", 1, 3600),
        default=DEFAULT_TIMEOUT,
        metavar="SEC",
        help=f"таймаут сокета и порог проверки чтения в секундах (по умолчанию {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="итог одним JSON-объектом в stdout; прогресс при этом идёт в stderr",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Точка входа; возвращает код возврата процесса."""
    args = parse_args(argv)
    total_bytes = 0
    total_seconds = 0.0
    completed = 0
    try:
        for attempt in range(1, args.count + 1):
            result = fetch_once(args.url, args.timeout)
            total_bytes += result.size
            total_seconds += result.elapsed
            completed = attempt
            note = f" (переадресаций: {result.redirects})" if result.redirects else ""
            # Прогресс — в stderr: stdout остаётся чистым для итога и автоматизации.
            print(
                f"Запрос {attempt}/{args.count}: {result.status}, "
                f"{human_bytes(result.size)} за {result.elapsed:.3f} с{note}",
                file=sys.stderr,
            )
    except (OSError, http.client.HTTPException, ValueError) as error:
        print(f"Запрос {completed + 1}/{args.count}: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\nИзмерение прервано на запросе {completed + 1}/{args.count}.", file=sys.stderr)
        return 130

    average_seconds = total_seconds / args.count
    speed = total_bytes / MIB / total_seconds if total_seconds else 0.0
    if args.json:
        print(
            json.dumps(
                {
                    "url": _absolute(args.url),
                    "requests_planned": args.count,
                    "requests_done": completed,
                    "total_bytes": total_bytes,
                    "total_seconds": round(total_seconds, 3),
                    "average_seconds": round(average_seconds, 3),
                    "speed_mib_per_second": round(speed, 3),
                },
                ensure_ascii=False,
            )
        )
        return 0

    pretty = f"{total_bytes:,}".replace(",", " ")
    print(f"Запросов: {args.count}")
    print(f"Скачано: {human_bytes(total_bytes)} ({pretty} байт)")
    print(f"Среднее время запроса: {average_seconds:.3f} с")
    print(f"Скорость скачивания: {speed:.3f} MiB/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
