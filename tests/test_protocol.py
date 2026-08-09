"""Parser must treat serial input as untrusted: parse the good, reject the bad, never crash."""
from datetime import datetime

import pytest

from roadsense.models import Hello, Packet
from roadsense.protocol import MalformedMessage, parse_line


def test_valid_telemetry():
    pkt = parse_line("T,123456,15600,3200,28.4,NORMAL")
    assert isinstance(pkt, Packet)
    assert pkt.kind == "T"
    assert not pkt.is_event
    assert pkt.arduino_time_ms == 123456
    assert pkt.ay == 15600
    assert pkt.shock == 3200
    assert pkt.distance_cm == pytest.approx(28.4)
    assert pkt.status == "NORMAL"
    assert pkt.raw == "T,123456,15600,3200,28.4,NORMAL"


def test_valid_event():
    pkt = parse_line("E,123456,15600,5200,34.8,POTHOLE")
    assert pkt.kind == "E"
    assert pkt.is_event
    assert pkt.status == "POTHOLE"
    assert pkt.distance_cm == pytest.approx(34.8)


def test_na_distance_becomes_none():
    pkt = parse_line("T,10,100,5,NA,NORMAL")
    assert pkt.distance_cm is None


def test_integer_distance_parses():
    pkt = parse_line("T,10,100,5,28,SPEED_BREAKER")
    assert pkt.distance_cm == pytest.approx(28.0)
    assert pkt.status == "SPEED_BREAKER"


def test_blank_and_whitespace_lines_ignored():
    assert parse_line("") is None
    assert parse_line("   \r\n") is None
    assert parse_line(None) is None


def test_trailing_crlf_stripped():
    pkt = parse_line("T,10,100,5,12.0,NORMAL\r\n")
    assert pkt.status == "NORMAL"
    assert "\r" not in pkt.raw and "\n" not in pkt.raw


def test_hello_handshake():
    hello = parse_line("HELLO,ROADSENSE,1")
    assert isinstance(hello, Hello)
    assert hello.version == "1"


def test_received_at_is_passed_through():
    ts = datetime(2026, 8, 9, 14, 32, 0)
    pkt = parse_line("T,10,100,5,12.0,NORMAL", received_at=ts)
    assert pkt.received_at == ts


@pytest.mark.parametrize(
    "line",
    [
        "T,10,100,5,NORMAL",              # too few fields
        "T,10,100,5,12.0,NORMAL,extra",   # too many fields
        "T,abc,100,5,12.0,NORMAL",        # non-numeric time
        "T,10,xx,5,12.0,NORMAL",          # non-numeric ay
        "T,10,100,yy,12.0,NORMAL",        # non-numeric shock
        "T,10,100,5,notnum,NORMAL",       # non-numeric distance
        "T,10,100,5,-4.0,NORMAL",         # negative distance
        "T,10,100,5,12.0,FLYING",         # unknown status
        "X,10,100,5,12.0,NORMAL",         # unknown message type
        "HELLO,SOMETHINGELSE,1",          # malformed handshake
        "garbage line",                   # nonsense
    ],
)
def test_malformed_lines_rejected(line):
    with pytest.raises(MalformedMessage) as exc:
        parse_line(line)
    assert exc.value.raw == line.strip()
    assert exc.value.reason  # a human-readable reason is attached
