"""Copy and paste, without a display: what each keypress and paste comes to."""

from __future__ import annotations

import pytest

from waprinter.ui import clipboard, theme


class TestShortcutsOnAnyKeyboard:
    @pytest.mark.parametrize("keycode,keysym,event", [
        (86, "Telugu_va", "<<Paste>>"),
        (67, "U+0C1A", "<<Copy>>"),
        (88, "??", "<<Cut>>"),
        (65, "Hindi_a", "<<SelectAll>>"),
    ])
    def test_the_physical_key_counts_when_the_letter_is_not_latin(self, keycode, keysym, event):
        assert clipboard.key_event(keycode, keysym) == event

    @pytest.mark.parametrize("keysym", ["v", "V", "c", "C", "x", "a"])
    def test_a_latin_layout_is_left_to_tks_own_bindings(self, keysym):
        assert clipboard.key_event(ord(keysym.upper()), keysym) is None

    def test_other_keys_do_nothing(self):
        assert clipboard.key_event(66, "Telugu_ba") is None


class TestPastingIntoAOneLineBox:
    @pytest.mark.parametrize("copied,kept", [
        ("9876543210\r\n", "9876543210"),
        ("  EAAG123  \n", "EAAG123"),
        ("first\r\nsecond\n", "first second"),
        ("\n\n", ""),
        ("plain", "plain"),
    ])
    def test_line_breaks_do_not_arrive(self, copied, kept):
        assert clipboard.clean_paste(copied) == kept


class TestFittingTheScreen:
    def test_a_window_that_fits_is_the_size_asked_for(self):
        assert theme.fitted_geometry((0, 0, 1920, 1040), 1180, 780, 40)[:2] == (1180, 780)

    @pytest.mark.parametrize("area", [
        (0, 0, 1024, 728),     # the smallest counter screen, taskbar along the bottom
        (0, 0, 1366, 728),
        (0, 40, 1366, 728),    # taskbar along the top
        (64, 0, 1302, 768),    # taskbar down the left
    ])
    def test_its_bottom_edge_stays_above_the_taskbar(self, area):
        left, top, width, height = area
        w, h, x, y = theme.fitted_geometry(area, 1180, 780, 40)
        assert left <= x and x + w <= left + width
        assert top <= y and y + h + 40 <= top + height
