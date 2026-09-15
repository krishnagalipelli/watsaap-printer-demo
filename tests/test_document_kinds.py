"""A chit fund prints more than receipts, and each kind has its own template.

Removal notices and removal letters go through the same printer queue as
receipts. They are different documents, to different people, saying different
things: one warns a member they are about to be removed, the other confirms
they have been. Sending either under the receipt wording -- "Thank you for
your payment" -- would be considerably worse than sending nothing.

The documents here are invented. Real notices carry a member's name, mobile
and the arrears they are behind on, which have no business in a repo.
"""

from __future__ import annotations

import pytest
from invoice_factory import (
    ChitReceiptSpec,
    RemovalLetterSpec,
    RemovalNoticeSpec,
    build_chit_receipt,
    build_removal_letter,
    build_removal_notice,
)


@pytest.fixture
def notice(tmp_path):
    def _make(spec: RemovalNoticeSpec | None = None):
        return build_removal_notice(spec or RemovalNoticeSpec(), tmp_path / "rn.pdf")

    return _make


@pytest.fixture
def letter(tmp_path):
    def _make(spec: RemovalLetterSpec | None = None):
        return build_removal_letter(spec or RemovalLetterSpec(), tmp_path / "rl.pdf")

    return _make


@pytest.fixture
def receipt(tmp_path):
    def _make(spec: ChitReceiptSpec | None = None):
        return build_chit_receipt(spec or ChitReceiptSpec(), tmp_path / "receipt.pdf")

    return _make


class TestRecognisingTheDocument:
    def test_a_removal_notice_is_recognised(self, link_pipeline, notice):
        job = link_pipeline.process_document(notice())[0]
        assert job.fields.document_kind == "removal_notice"

    def test_a_removal_letter_is_recognised(self, link_pipeline, letter):
        job = link_pipeline.process_document(letter())[0]
        assert job.fields.document_kind == "removal_letter"

    def test_a_receipt_is_still_just_a_receipt(self, link_pipeline, receipt):
        """No kind, and therefore the default template. Unchanged behaviour."""
        job = link_pipeline.process_document(receipt())[0]
        assert job.fields.document_kind is None
        assert job.template_name == "chit_receipt"


class TestSendingItUnderTheRightTemplate:
    def test_a_notice_goes_out_as_a_notice(self, link_pipeline, notice):
        job = link_pipeline.process_document(notice())[0]

        assert job.template_name == "removal_notice"
        assert "Removal Notice has been issued" in job.message_preview
        assert "Thank you for your payment" not in job.message_preview

    def test_a_letter_goes_out_as_a_letter(self, link_pipeline, letter):
        job = link_pipeline.process_document(letter())[0]

        assert job.template_name == "removal_letter"
        assert "Removal Letter has been issued" in job.message_preview

    def test_the_notice_carries_its_own_number_and_date(self, link_pipeline, notice):
        job = link_pipeline.process_document(
            notice(RemovalNoticeSpec(notice_number="RN901/26", notice_date="14-Oct-2026"))
        )[0]

        assert "Notice No.: RN901/26" in job.message_preview
        assert "Date: 14-Oct-2026" in job.message_preview

    def test_the_letter_carries_its_own_number(self, link_pipeline, letter):
        job = link_pipeline.process_document(
            letter(RemovalLetterSpec(letter_number="RL777/26"))
        )[0]

        assert "Letter No.: RL777/26" in job.message_preview

    def test_it_reaches_the_member_on_the_document(self, link_pipeline, notice):
        job = link_pipeline.process_document(
            notice(RemovalNoticeSpec(member_phone="9876500011"))
        )[0]

        assert job.recipient == "+919876500011"


class TestGreetingTheRightPerson:
    def test_the_notice_greets_the_member_by_name(self, link_pipeline, notice):
        job = link_pipeline.process_document(
            notice(RemovalNoticeSpec(member_name="Mr RAGHAVA RAO"))
        )[0]

        assert job.fields.customer_name == "Mr RAGHAVA RAO"
        assert job.message_preview.startswith("Dear Mr RAGHAVA RAO,")

    def test_the_letter_greets_the_member_by_name(self, link_pipeline, letter):
        job = link_pipeline.process_document(
            letter(RemovalLetterSpec(member_name="SUNITHA REDDY"))
        )[0]

        assert job.message_preview.startswith("Dear SUNITHA REDDY,")

    def test_the_act_is_not_mistaken_for_a_member(self, link_pipeline, notice):
        """"...removed from the list of subscribers in terms of the provisions
        of the Chit Fund Act" -- "subscriber" is a customer anchor, and it used
        to match inside "subscribers", greeting the member with the sentence.
        """
        job = link_pipeline.process_document(notice())[0]

        assert "Chit fund Act" not in (job.fields.customer_name or "")
        assert "provisions" not in job.message_preview

    def test_the_salutation_is_not_mistaken_for_a_member(self, link_pipeline, notice):
        """"Dear Sir/ Madam," is printed between "To," and the member's name."""
        job = link_pipeline.process_document(notice())[0]

        assert "Dear Dear" not in job.message_preview


class TestWhatTheMemberSeesBeforeOpeningIt:
    def test_a_notice_is_not_attached_as_a_receipt(self, pipeline, notice):
        """The filename is read before the PDF is. A removal notice arriving as
        "Receipt-RN317-26.pdf" is the same mistake as sending "Invoice-"."""
        from waprinter.extract import extract_fields
        from waprinter.send.templates import render

        path = notice()
        fields = extract_fields(path)
        name, template = pipeline.template_for(fields)
        message = render(
            template, pipeline.settings.template_variables, fields,
            document_noun=pipeline.noun_for(fields),
        )

        assert message.filename == "Removal Notice-RN317-26.pdf"

    def test_a_receipt_is_still_attached_as_a_receipt(self, pipeline, receipt):
        from waprinter.extract import extract_fields
        from waprinter.send.templates import render

        fields = extract_fields(receipt())
        _, template = pipeline.template_for(fields)
        message = render(
            template, pipeline.settings.template_variables, fields,
            document_noun=pipeline.noun_for(fields),
        )

        assert message.filename.startswith("Receipt-")


class TestTheShippedDefaults:
    """Guards the configuration the branches actually run on.

    Every template variable has to resolve from what the shipped Settings
    map, because an unresolved one does not fail -- it sends as "-". A member
    receiving "Notice No.: -" is worse than one receiving nothing.
    """

    @pytest.mark.parametrize(
        "kind, build, spec",
        [
            ("removal_notice", build_removal_notice, RemovalNoticeSpec()),
            ("removal_letter", build_removal_letter, RemovalLetterSpec()),
        ],
    )
    def test_every_variable_resolves(self, tmp_path, kind, build, spec):
        from waprinter.config import Settings
        from waprinter.extract import extract_fields
        from waprinter.send.templates import TemplateStore, render

        settings = Settings()
        templates = TemplateStore(tmp_path / "templates.json")

        fields = extract_fields(build(spec, tmp_path / f"{kind}.pdf"))
        assert fields.document_kind == kind

        name = settings.document_templates[kind]
        message = render(
            templates.get(name), settings.template_variables, fields,
            extra={"business_name": settings.business_name},
            document_noun=settings.document_nouns[kind],
        )

        assert message.missing == [], (
            f"{name} would send these as '-': {message.missing}"
        )
        assert "{{" not in message.preview


class TestReachingMachinesThatAreAlreadyRunning:
    """A template added in a release has to arrive on an existing install.

    templates.json used to be the whole truth, so a machine that had already
    printed something kept the template list it was installed with. The first
    removal notice would have been held on every counter at once with
    "Template 'removal_notice' is not configured" -- the same failure as a
    database column that never reached the machines that needed it.
    """

    @pytest.fixture
    def installed_before(self, tmp_path):
        """templates.json as written before the removal templates existed."""
        import json

        path = tmp_path / "templates.json"
        path.write_text(
            json.dumps(
                {
                    "templates": [
                        {
                            "name": "chit_receipt",
                            "language": "en",
                            "body": "Dear {{1}}, thank you for your payment.",
                            "status": "approved",
                            "category": "UTILITY",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_a_new_template_reaches_an_existing_install(self, installed_before):
        from waprinter.send.templates import TemplateStore

        store = TemplateStore(installed_before)

        assert store.get("removal_notice") is not None
        assert store.get("removal_letter") is not None

    def test_what_the_machine_has_stored_still_wins(self, tmp_path):
        """Meta's verdict outranks the build's optimism.

        A template Meta rejected must not be quietly made approved again by
        shipping a new version of the app.
        """
        import json

        from waprinter.send.templates import TemplateStore

        path = tmp_path / "templates.json"
        path.write_text(
            json.dumps(
                {
                    "templates": [
                        {
                            "name": "removal_notice",
                            "language": "en",
                            "body": "whatever Meta actually has",
                            "status": "rejected",
                            "category": "UTILITY",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        assert TemplateStore(path).get("removal_notice").status == "rejected"

    def test_a_notice_is_held_rather_than_sent_as_a_receipt(
        self, link_pipeline, tmp_path
    ):
        """If the template really is missing, holding is the only safe answer."""
        link_pipeline.settings.document_templates = {"removal_notice": "not_created_yet"}
        path = build_removal_notice(RemovalNoticeSpec(), tmp_path / "rn.pdf")

        job = link_pipeline.process_document(path)[0]

        assert job.status.value == "held"
        assert "not_created_yet" in job.hold_reason
        assert job.message_preview is None


class TestWhenTheDocumentArrivesAsAScan:
    """The same three documents, printed as an image with no text layer.

    A branch that scans scans everything, so the title that says which
    document this is arrives as OCR output like the rest of the page. Getting
    it wrong picks the wrong approved template, which writes to the right
    member with the wrong words -- worse than a misread number, which at least
    reaches someone who can see the message was not meant for them.
    """

    @pytest.fixture(autouse=True)
    def _needs_tesseract(self):
        from waprinter.extract.ocr import OcrSettings, available

        if not available(OcrSettings()):
            pytest.skip("Tesseract is not installed")

    def test_a_scanned_notice_is_still_read_as_a_notice(self, link_pipeline, tmp_path):
        path = build_removal_notice(
            RemovalNoticeSpec(raster=True), tmp_path / "scan_rn.pdf"
        )
        job = link_pipeline.process_document(path)[0]

        assert job.fields.used_ocr is True
        assert job.fields.document_kind == "removal_notice"
        assert link_pipeline.template_for(job.fields)[0] == "removal_notice"
        assert link_pipeline.noun_for(job.fields) == "Removal Notice"

    def test_a_scanned_letter_is_still_read_as_a_letter(self, link_pipeline, tmp_path):
        path = build_removal_letter(
            RemovalLetterSpec(raster=True), tmp_path / "scan_rl.pdf"
        )
        job = link_pipeline.process_document(path)[0]

        assert job.fields.document_kind == "removal_letter"
        assert link_pipeline.template_for(job.fields)[0] == "removal_letter"

    def test_a_scanned_receipt_is_identified_rather_than_assumed(
        self, link_pipeline, tmp_path
    ):
        """The pre-printed wording is on the paper even though the PDF's text
        layer never carries it, so a scan is the one place a receipt can be
        recognised positively instead of by matching nothing."""
        path = build_chit_receipt(
            ChitReceiptSpec(raster=True, preprinted_wording=True),
            tmp_path / "scan_cr.pdf",
        )
        job = link_pipeline.process_document(path)[0]

        assert job.fields.document_kind == "receipt"
        assert link_pipeline.template_for(job.fields)[0] == "chit_receipt"

    def test_it_still_reaches_the_member_on_the_page(self, link_pipeline, tmp_path):
        path = build_removal_notice(
            RemovalNoticeSpec(raster=True, member_phone="9876500011"),
            tmp_path / "scan_rn2.pdf",
        )
        job = link_pipeline.process_document(path)[0]

        assert job.recipient == "+919876500011"

    def test_releasing_a_held_scan_sends_the_notice_wording(
        self, link_pipeline, tmp_path
    ):
        """Every scanned send is held by default, so this is the path a real
        removal notice actually takes: read by OCR, held, released by the
        operator. The template it leaves under is decided then, not at print."""
        path = build_removal_notice(
            RemovalNoticeSpec(raster=True), tmp_path / "scan_rn5.pdf"
        )
        job = link_pipeline.process_document(path)[0]
        assert job.status.value == "held"

        released = link_pipeline.release(job.id, "+919876500011")

        assert released.template_name == "removal_notice"
        assert "Removal Notice has been issued" in released.message_preview
        assert "Thank you for your payment" not in released.message_preview

    def test_two_reads_that_disagree_are_held(
        self, link_pipeline, tmp_path, monkeypatch
    ):
        """The number is read twice at different resolutions and demoted if
        the reads differ. The title now gets the same treatment, because it
        decides more."""
        from waprinter.extract.profile import DocumentProfile

        answers = iter([_kind("removal_notice"), None])
        monkeypatch.setattr(
            DocumentProfile, "kind_of", lambda self, text: next(answers, None)
        )

        path = build_removal_notice(
            RemovalNoticeSpec(raster=True), tmp_path / "scan_rn3.pdf"
        )
        job = link_pipeline.process_document(path)[0]

        assert job.fields.document_kind_verified is False
        assert job.status.value == "held"
        assert "two different answers" in job.hold_reason

    def test_an_unreadable_title_does_not_fall_back_to_the_receipt(
        self, link_pipeline, tmp_path, monkeypatch
    ):
        """Both reads finding nothing is not agreement that this is a receipt.

        With OCR trusted for silent sending, the only thing standing between a
        removal notice whose title did not survive the scan and "thank you for
        your payment" is this hold.
        """
        from waprinter.extract.profile import DocumentProfile

        monkeypatch.setattr(DocumentProfile, "kind_of", lambda self, text: None)
        link_pipeline.settings.ocr_silent_send = True

        path = build_removal_notice(
            RemovalNoticeSpec(raster=True), tmp_path / "scan_rn4.pdf"
        )
        job = link_pipeline.process_document(path)[0]

        assert job.status.value == "held"
        assert "could not be identified" in job.hold_reason

    def test_an_install_with_one_message_has_nothing_to_get_wrong(
        self, link_pipeline, tmp_path, monkeypatch
    ):
        """The hold above is about confusing two documents. A client who only
        ever sends receipts is not made to confirm every scan."""
        from waprinter.extract.profile import DocumentProfile

        monkeypatch.setattr(DocumentProfile, "kind_of", lambda self, text: None)
        link_pipeline.settings.ocr_silent_send = True
        link_pipeline.settings.document_templates = {}

        path = build_chit_receipt(
            ChitReceiptSpec(raster=True), tmp_path / "scan_cr2.pdf"
        )
        job = link_pipeline.process_document(path)[0]

        assert job.status.value != "held"
        assert job.template_name == "chit_receipt"


def _kind(name: str):
    from waprinter.extract.profile import DocumentKind

    return DocumentKind(name=name)
