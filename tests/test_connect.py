"""Connecting an account from the token alone, and keeping templates honest.

The token already knows which WhatsApp Business Account it belongs to and
that account knows its phone numbers, so nobody types a fifteen-digit ID.
And a refresh makes the local copy say what Meta says: every template, every
language, and nothing still marked approved after it was deleted.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from waprinter.config import Settings
from waprinter.send.meta_account import MetaError, inspect
from waprinter.send.templates import MessageTemplate, TemplateStore

GRAPH = "https://graph.facebook.com/v21.0"
TOKEN = "EAAG" + "x" * 60


def debug(expires_at=0, waba_ids=("555",), valid=True):
    scopes = [{"scope": "whatsapp_business_messaging", "target_ids": list(waba_ids)}]
    return httpx.Response(200, json={"data": {
        "is_valid": valid, "expires_at": expires_at, "application": "Counter app",
        "granular_scopes": scopes if waba_ids else [],
    }})


class TestLookingUpAToken:
    @respx.mock
    def test_the_account_and_numbers_come_from_the_token(self):
        respx.get(f"{GRAPH}/debug_token").mock(return_value=debug())
        respx.get(f"{GRAPH}/555").mock(return_value=httpx.Response(
            200, json={"id": "555", "name": "Srinidhi"}))
        numbers = respx.get(f"{GRAPH}/555/phone_numbers").mock(side_effect=[
            httpx.Response(200, json={
                "data": [{"id": "111", "display_phone_number": "+91 87822 51999",
                          "verified_name": "Srinidhi Chit Funds"}],
                "paging": {"next": "x", "cursors": {"after": "p2"}}}),
            httpx.Response(200, json={"data": [{"id": "222", "display_phone_number": "+91 90000 00000"}]}),
        ])

        connection = inspect(TOKEN)

        assert [a.id for a in connection.accounts] == ["555"]
        account = connection.accounts[0]
        assert account.name == "Srinidhi"
        assert [n.id for n in account.phone_numbers] == ["111", "222"]
        assert account.phone_numbers[0].label == "+91 87822 51999 · Srinidhi Chit Funds"
        assert numbers.calls[1].request.url.params["after"] == "p2"
        assert connection.expiry_text() == "This token does not expire."

    @respx.mock
    def test_a_temporary_token_is_called_out(self):
        tomorrow = int(time.time()) + 20 * 3600
        respx.get(f"{GRAPH}/debug_token").mock(return_value=debug(expires_at=tomorrow))
        respx.get(f"{GRAPH}/555").mock(return_value=httpx.Response(200, json={"name": "S"}))
        respx.get(f"{GRAPH}/555/phone_numbers").mock(
            return_value=httpx.Response(200, json={"data": []}))

        connection = inspect(TOKEN)

        assert connection.short_lived
        assert "temporary token" in connection.expiry_text()

    @respx.mock
    def test_a_token_that_does_not_name_its_account_asks_for_it_once(self):
        respx.get(f"{GRAPH}/debug_token").mock(return_value=debug(waba_ids=()))
        connection = inspect(TOKEN)
        assert connection.accounts == []
        assert "Enter the account ID" in connection.problems[0]

    @respx.mock
    def test_the_account_id_given_by_hand_is_then_used(self):
        respx.get(f"{GRAPH}/debug_token").mock(return_value=debug(waba_ids=()))
        respx.get(f"{GRAPH}/777").mock(return_value=httpx.Response(200, json={"name": "S"}))
        respx.get(f"{GRAPH}/777/phone_numbers").mock(return_value=httpx.Response(
            200, json={"data": [{"id": "9"}]}))
        assert inspect(TOKEN, account_id="777").accounts[0].phone_numbers[0].id == "9"

    @respx.mock
    def test_an_expired_token_says_so_in_words(self):
        respx.get(f"{GRAPH}/debug_token").mock(return_value=httpx.Response(
            400, json={"error": {"code": 190, "message": "Error validating access token"}}))
        with pytest.raises(MetaError, match="not valid"):
            inspect(TOKEN)

    @respx.mock
    def test_no_network_is_a_sentence_not_a_traceback(self):
        respx.get(f"{GRAPH}/debug_token").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(MetaError, match="internet connection"):
            inspect(TOKEN)


class TestRefreshingTemplates:
    @respx.mock
    def test_examples_and_header_come_with_the_template(self, tmp_path):
        from waprinter.send.sync import sync_templates

        respx.get(f"{GRAPH}/555/message_templates").mock(return_value=httpx.Response(200, json={
            "data": [
                {"name": "member_statement", "language": "en", "status": "APPROVED",
                 "parameter_format": "NAMED",
                 "components": [
                     {"type": "HEADER", "format": "DOCUMENT"},
                     {"type": "BODY", "text": "Dear {{name}}, member {{id}}.",
                      "example": {"body_text_named_params": [
                          {"param_name": "name", "example": "Ravi"},
                          {"param_name": "id", "example": "SCF-1"}]}}]},
                {"name": "promo", "language": "en", "status": "APPROVED",
                 "components": [{"type": "HEADER", "format": "IMAGE"},
                                {"type": "BODY", "text": "Hi {{1}}",
                                 "example": {"body_text": [["Ravi"]]}}]},
            ]}))
        store = TemplateStore(tmp_path / "templates.json")
        assert sync_templates(Settings(business_account_id="555"), store, TOKEN) == 2

        statement = store.get("member_statement")
        assert statement.examples == {"name": "Ravi", "id": "SCF-1"}
        assert statement.usable
        promo = store.get("promo")
        assert promo.examples == {"1": "Ravi"}
        assert promo.header_format == "IMAGE" and not promo.usable
        assert store.refreshed_at

    @respx.mock
    def test_a_template_deleted_on_whatsapp_stops_being_usable_here(self, tmp_path):
        from waprinter.send.sync import sync_templates

        store = TemplateStore(tmp_path / "templates.json")
        store.put(MessageTemplate(name="old_one", status="approved"))
        respx.get(f"{GRAPH}/555/message_templates").mock(
            return_value=httpx.Response(200, json={"data": []}))
        sync_templates(Settings(business_account_id="555"), store, TOKEN)

        old = TemplateStore(tmp_path / "templates.json").get("old_one")
        assert old is not None, "the words are kept"
        assert old.status == "missing" and not old.usable

    def test_one_name_in_two_languages_is_two_templates(self, tmp_path):
        store = TemplateStore(tmp_path / "templates.json", language="te")
        store.put(MessageTemplate(name="statement", language="en", status="approved"))
        store.put(MessageTemplate(name="statement", language="te", status="approved"))

        reloaded = TemplateStore(tmp_path / "templates.json", language="te")
        assert reloaded.get("statement").language == "te"
        assert reloaded.get("statement@en").language == "en"
        assert reloaded.ref_for(reloaded.get("statement@en")) == "statement@en"
        assert reloaded.ref_for(reloaded.get("invoice_document")) == "invoice_document"


class TestConnectCommand:
    def test_one_number_is_chosen_and_templates_loaded(self, monkeypatch, capsys):
        from waprinter import cli
        from waprinter.send.meta_account import BusinessAccount, Connection, PhoneNumber

        monkeypatch.setattr(cli, "_read_token", lambda f: TOKEN)
        monkeypatch.setattr("waprinter.send.meta_account.inspect",
                            lambda *a, **k: Connection(valid=True, accounts=[
                                BusinessAccount("555", "S", [PhoneNumber("111", "+91 1")])]))
        monkeypatch.setattr("waprinter.send.sync.sync_templates", lambda *a, **k: 4)

        assert cli.main(["connect"]) == 0
        out = capsys.readouterr().out
        assert "Connected: +91 1" in out and "Loaded 4 template(s)" in out
        settings = Settings.load()
        assert (settings.phone_number_id, settings.business_account_id) == ("111", "555")
        from waprinter.secrets import load_token
        assert load_token() == TOKEN

    def test_several_numbers_need_a_choice(self, monkeypatch, capsys):
        from waprinter import cli
        from waprinter.send.meta_account import BusinessAccount, Connection, PhoneNumber

        monkeypatch.setattr(cli, "_read_token", lambda f: TOKEN)
        monkeypatch.setattr("waprinter.send.meta_account.inspect",
                            lambda *a, **k: Connection(valid=True, accounts=[
                                BusinessAccount("555", "S", [PhoneNumber("1"), PhoneNumber("2")])]))
        assert cli.main(["connect"]) == 1
        assert "--phone" in capsys.readouterr().out
        assert Settings.load().phone_number_id == ""


class TestTheTokenStaysOutOfTheLog:
    @respx.mock
    def test_connecting_does_not_write_the_token_to_waprinter_log(self, tmp_path):
        """debug_token takes the token in the URL, and httpx logs every URL
        at INFO -- the level the agent's log file runs at."""
        import logging

        from waprinter.runner import configure_logging

        root = logging.getLogger()
        before = list(root.handlers)
        httpx_level = logging.getLogger("httpx").level
        try:
            configure_logging(tmp_path, logging.INFO)
            respx.get(f"{GRAPH}/debug_token").mock(return_value=debug(waba_ids=()))
            inspect(TOKEN)
            for handler in root.handlers:
                handler.flush()
            assert TOKEN not in (tmp_path / "waprinter.log").read_text()
        finally:
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)
                    handler.close()
            logging.getLogger("httpx").setLevel(httpx_level)
