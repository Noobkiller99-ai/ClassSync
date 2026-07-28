"""
tests/test_spp_mandatory.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tests for SPP Salesforce platform session number & course name parsing,
matching mandatory sessions by course name/code, and displaying course name
on uploaded course outlines.
"""
import io
from datetime import datetime
from unittest.mock import patch

import pytest

from class_sync.models import TimetableEvent
from class_sync.spp import parse_spp_sessions
from class_sync.store import (
    check_is_mandatory,
    get_mandatory_sessions,
    save_mandatory_sessions,
)
from class_sync.wisenet import MandatorySessionInfo, parse_mandatory_sessions_from_pdf


def test_spp_parse_session_number_and_course_name():
    raw_sessions = [
        {
            "id": "sess-1",
            "courseName": "Management Control System",
            "sessionDate": "2026-07-28",
            "startTime": "9:00AM",
            "endTime": "10:15AM",
            "courseActivity": "Session",
            "instructorNames": "Prof. Smith",
            "title": "Session 17",
        }
    ]
    events = parse_spp_sessions(raw_sessions)
    assert len(events) == 1
    e = events[0]
    assert e.subject_name == "Management Control System"
    assert e.session_number == "17"
    assert e.faculty == "Prof. Smith"


def test_check_is_mandatory_matching_by_course_name():
    mandatory_data = {
        "FIN521": {
            "sessions": [17, 18],
            "course_name": "Management Control System",
        }
    }

    # Match by course name for SPP (where course_code is empty)
    assert check_is_mandatory("", "Management Control System", "17", mandatory_data) is True
    assert check_is_mandatory("", "Management Control System", "19", mandatory_data) is False

    # Match by course code for TCS
    assert check_is_mandatory("FIN521-PDM", "Other Title", "17", mandatory_data) is True
    assert check_is_mandatory("FIN521", "Other Title", "19", mandatory_data) is False


def test_wisenet_extracts_course_name_from_pdf():
    pdf_text = (
        "PGDM 2025 - 2027 SPJIMR - Course outline\n"
        "Course Name Management Control System Credits 3\n"
        "Course Code FIN521 Term IV\n"
    )
    from unittest.mock import MagicMock
    mock_pdf = MagicMock()
    mock_page = MagicMock()
    mock_page.extract_text.return_value = pdf_text
    mock_page.extract_tables.return_value = []
    mock_pdf.pages = [mock_page]
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_pdf

    with patch("pdfplumber.open", return_value=mock_cm):
        info = parse_mandatory_sessions_from_pdf(b"dummy pdf bytes", "FIN521")
        assert info.course_code == "FIN521"
        assert info.course_name == "Management Control System"


def test_spp_classroom_extraction_and_payload():
    from class_sync.spp import spp_google_payload
    raw_sessions = [
        {
            "id": "sess-2",
            "courseName": "Corporate Finance",
            "sessionDate": "2026-07-28",
            "startTime": "2:00PM",
            "endTime": "3:15PM",
            "courseActivity": "Session",
            "instructorNames": "Prof. Jones",
            "title": "5",
            "classroom": "Lab 3B",
        }
    ]
    events = parse_spp_sessions(raw_sessions)
    assert len(events) == 1
    e = events[0]
    assert e.classroom == "Lab 3B"

    payload = spp_google_payload(e)
    assert payload.get("location") == "Lab 3B"
