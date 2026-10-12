"""Sending a print as an image of itself instead of as a PDF.

A PDF reaches the member as a file they have to tap, and on a phone with no
PDF viewer that tap goes nowhere. An image is shown in the chat and is read
without opening anything, so each kind of paperwork can now choose.

What the app cannot choose is Meta's half of it: a template header holds a
document or an image, settled when the template was approved. Everything here
about refusing, holding and saying why exists because that mismatch has to be
visible in setup rather than as a receipt that never arrived.
"""

from __future__ import annotations

import json

import httpx
import pymupdf
import pytest
import respx

from waprinter.models import ExtractedFields, JobStatus
from waprinter.send.attachment import (
    IMAGE,
    PDF,
    ConversionError,
    format_for,
    prepared,
    to_jpeg,
)
from waprinter.send.templates import MessageTemplate, render
from waprinter.send.whatsapp import WhatsAppCloudSender

PHONE_ID = "123456789"
BASE = f"https://graph.facebook.com/v21.0/{PHONE_ID}"
JPEG_MAGIC = b"\xff\xd8\xff"


def pages(count: int, out_path, width=400, height=600):
    """A PDF of `count` numbered pages, each with something on it."""
    with pymupdf.open() as doc:
        for number in range(count):
            page = doc.new_page(width=width, height=height)
            page.insert_text((40, 80), f"Page {number + 1}", fontsize=24)
        doc.save(str(out_path))
    return out_path


def pixels(path) -> tuple[int, int]:
    """The JPEG's own width and height, read back from the file."""
    with pymupdf.open(str(path)) as doc:
        rect = doc.load_page(0).rect
        return round(rect.width), round(rect.height)


@pytest.fixture
def image_template():
    """An approved template whose header takes an image, as Meta reports it."""
    return MessageTemplate(
        name="receipt_image",
        language="en",
        body="Dear {{1}}, receipt {{2}} is attached.",
        status="approved",
        header_document=False,
        header_format="IMAGE",
    )


@pytest.fixture
def receipt_fields():
    return ExtractedFields(invoice_number="CR1747/26", customer_name="Ravi")


@pytest.fixture
def image_message(image_template, receipt_fields):
    return render(
        image_template,
        {"1": "customer_name", "2": "invoice_number"},
        receipt_fields,
        document_noun="Receipt",
        attachment=IMAGE,
    )


@pytest.fixture
def sender():
    return WhatsAppCloudSender(PHONE_ID, "test-token", api_version="v21.0")


class TestConverting:
    def test_one_page_becomes_one_jpeg(self, tmp_path):
        out = to_jpeg(pages(1, tmp_path / "one.pdf"), tmp_path / "one.jpg")

        assert out.read_bytes()[:3] == JPEG_MAGIC
        assert out.stat().st_size > 1000

    def test_every_page_is_in_it(self, tmp_path):
        """A three-page print is one image three pages tall, not page one."""
        one = to_jpeg(pages(1, tmp_path / "one.pdf"), tmp_path / "one.jpg")
        three = to_jpeg(pages(3, tmp_path / "three.pdf"), tmp_path / "three.jpg")

        width, height = pixels(three)
        assert (width, height) == (pixels(one)[0], pixels(one)[1] * 3)

    def test_pages_of_different_sizes_keep_their_own(self, tmp_path):
        """A narrow page is not stretched to the width of a wide one.

        Scaling each page to the widest would show one print at two scales,
        which on paperwork carrying figures reads as two documents.
        """
        source = tmp_path / "mixed.pdf"
        with pymupdf.open() as doc:
            doc.new_page(width=400, height=600)
            doc.new_page(width=300, height=200)
            doc.save(str(source))

        width, height = pixels(to_jpeg(source, tmp_path / "mixed.jpg"))

        assert width / height == pytest.approx(400 / 800, rel=0.01)

    def test_a_big_page_is_shrunk_until_it_fits(self, tmp_path):
        """Resolution is given up to stay under Meta's limit, not the send."""
        source = tmp_path / "dense.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            for row in range(60):
                page.insert_text((20, 20 + row * 12), "9876543210 " * 6, fontsize=9)
            doc.save(str(source))

        full = to_jpeg(source, tmp_path / "full.jpg")
        capped = to_jpeg(source, tmp_path / "capped.jpg", max_bytes=120_000)

        assert capped.stat().st_size <= 120_000 < full.stat().st_size

    def test_one_that_cannot_fit_says_so_instead_of_sending_rubbish(self, tmp_path):
        with pytest.raises(ConversionError) as raised:
            to_jpeg(pages(1, tmp_path / "one.pdf"), tmp_path / "one.jpg", max_bytes=200)

        assert "over WhatsApp's" in str(raised.value)
        assert "as a PDF" in str(raised.value)
        assert not (tmp_path / "one.jpg").exists()

    def test_a_pdf_with_no_pages_is_a_conversion_error(self, tmp_path):
        """A print that captured nothing is held, not written as an empty JPEG."""
        source = tmp_path / "empty.pdf"
        source.write_bytes(
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
            b"trailer<</Root 1 0 R>>\n"
        )

        with pytest.raises(ConversionError) as raised:
            to_jpeg(source, tmp_path / "empty.jpg")

        assert "no pages" in str(raised.value)

    def test_an_unreadable_file_raises_ours_not_pymupdfs(self, tmp_path):
        """One exception type for a caller to catch and show an operator."""
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"this is not a PDF")

        with pytest.raises(ConversionError) as raised:
            to_jpeg(broken, tmp_path / "broken.jpg")

        assert "broken.pdf" in str(raised.value)


class TestWhatGoesOut:
    def test_a_pdf_send_hands_over_the_captured_file_untouched(self, tmp_path):
        source = pages(1, tmp_path / "one.pdf")

        with prepared(source, PDF, "Receipt-CR1747-26.pdf") as (path, media_type):
            assert path == source
            assert media_type == "application/pdf"

        assert source.exists()

    def test_an_image_send_keeps_the_jpeg_only_for_the_upload(self, tmp_path):
        source = pages(1, tmp_path / "one.pdf")

        with prepared(source, IMAGE, "Receipt-CR1747-26.jpg") as (path, media_type):
            assert media_type == "image/jpeg"
            assert path.name == "Receipt-CR1747-26.jpg"
            assert path.read_bytes()[:3] == JPEG_MAGIC
            kept = path

        # The PDF is the record of what was printed; the image is not kept.
        assert not kept.exists()
        assert source.exists()

    def test_an_unknown_format_means_pdf(self):
        """A typo in settings.json must not quietly stop a receipt going out."""
        assert format_for("jpeg") == PDF
        assert format_for("") == PDF
        assert format_for(None) == PDF
        assert format_for(" IMAGE ") == IMAGE


class TestTheTemplateHasToBeAbleToCarryIt:
    def test_an_image_header_carries_an_image_and_not_a_pdf(self, image_template):
        assert image_template.carries(IMAGE)
        assert not image_template.carries(PDF)
        assert image_template.usable_for(IMAGE)
        assert not image_template.usable  # which still means "can send the PDF"

    def test_a_document_header_carries_a_pdf_and_not_an_image(self):
        template = MessageTemplate(name="t", status="approved", header_format="DOCUMENT")

        assert template.usable_for(PDF)
        assert not template.usable_for(IMAGE)

    def test_approval_is_still_required(self, image_template):
        image_template.status = "pending"

        assert not image_template.usable_for(IMAGE)

    def test_the_file_is_named_for_what_it_is(self, image_message):
        assert image_message.filename == "Receipt-CR1747-26.jpg"
        assert image_message.attachment == IMAGE


class TestSendingAnImage:
    @respx.mock
    def test_uploads_a_jpeg_and_references_it_in_the_image_header(
        self, sender, tmp_path, image_message
    ):
        source = pages(1, tmp_path / "receipt.pdf")
        upload = respx.post(f"{BASE}/media").mock(
            return_value=httpx.Response(200, json={"id": "media-img"})
        )
        send = respx.post(f"{BASE}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.IMG"}]})
        )

        result = sender.send("+919876543210", source, image_message)

        assert result.ok and result.wamid == "wamid.IMG"
        posted = upload.calls[0].request.read()
        assert b"image/jpeg" in posted
        assert JPEG_MAGIC in posted
        assert b"%PDF" not in posted  # the PDF itself never left

        header = json.loads(send.calls[0].request.read())["template"]["components"][0]
        assert header["parameters"][0] == {"type": "image", "image": {"id": "media-img"}}

    @respx.mock
    def test_the_body_still_carries_the_receipt_number(
        self, sender, tmp_path, image_message
    ):
        """An image header shows no filename, so the body has to name it."""
        respx.post(f"{BASE}/media").mock(
            return_value=httpx.Response(200, json={"id": "media-img"})
        )
        send = respx.post(f"{BASE}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.IMG"}]})
        )

        sender.send("+919876543210", pages(1, tmp_path / "r.pdf"), image_message)

        body = json.loads(send.calls[0].request.read())["template"]["components"][1]
        assert [p["text"] for p in body["parameters"]] == ["Ravi", "CR1747/26"]

    @respx.mock
    def test_a_document_header_is_refused_before_anything_is_uploaded(
        self, sender, tmp_path, receipt_fields
    ):
        template = MessageTemplate(
            name="chit_receipt", body="Dear {{1}}", status="approved",
            header_format="DOCUMENT",
        )
        message = render(template, {"1": "customer_name"}, receipt_fields,
                         attachment=IMAGE)
        upload = respx.post(f"{BASE}/media")

        result = sender.send("+919876543210", pages(1, tmp_path / "r.pdf"), message)

        assert not result.ok and not result.retryable
        assert "an image cannot be attached" in result.error
        assert "a document header" in result.error
        assert not upload.called

    @respx.mock
    def test_a_conversion_failure_is_not_retried(self, sender, tmp_path, image_message):
        """Rendering again produces the same bytes, so a retry is a wasted print."""
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"not a PDF at all")
        upload = respx.post(f"{BASE}/media")

        result = sender.send("+919876543210", broken, image_message)

        assert not result.ok and not result.retryable
        assert "broken.pdf" in result.error
        assert not upload.called

    @respx.mock
    def test_the_pdf_route_is_unchanged(self, sender, tmp_path, receipt_fields):
        template = MessageTemplate(name="chit_receipt", body="Dear {{1}}",
                                   status="approved")
        message = render(template, {"1": "customer_name"}, receipt_fields,
                         document_noun="Receipt")
        upload = respx.post(f"{BASE}/media").mock(
            return_value=httpx.Response(200, json={"id": "media-pdf"})
        )
        send = respx.post(f"{BASE}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "wamid.PDF"}]})
        )

        result = sender.send("+919876543210", pages(1, tmp_path / "r.pdf"), message)

        assert result.ok
        assert b"application/pdf" in upload.calls[0].request.read()
        header = json.loads(send.calls[0].request.read())["template"]["components"][0]
        assert header["parameters"][0]["document"] == {
            "id": "media-pdf",
            "filename": "Receipt-CR1747-26.pdf",
        }


class TestTheCounterChoosesPerDocument:
    """Receipts can go as images while notices stay PDFs, and the setting
    that says so is read per kind of paperwork."""

    def test_pdf_unless_someone_chose_otherwise(self, pipeline, make_invoice):
        job = pipeline.process(make_invoice())

        _wanted, message = pipeline.compose(job)
        assert message.attachment == PDF
        assert message.filename.endswith(".pdf")

    def test_the_default_covers_receipts_and_anything_unrecognised(
        self, pipeline, make_invoice
    ):
        pipeline.settings.attachment_format = IMAGE

        job = pipeline.process(make_invoice())

        _wanted, message = pipeline.compose(job)
        assert message.attachment == IMAGE
        assert message.filename.endswith(".jpg")

    def test_one_kind_can_differ_from_the_rest(self, pipeline, make_invoice):
        pipeline.settings.attachment_format = IMAGE
        pipeline.settings.document_attachments = {"removal_notice": PDF}

        job = pipeline.process(make_invoice())
        job.fields.document_kind = "removal_notice"

        assert pipeline.attachment_for(job.fields) == PDF

    def test_a_dry_run_renders_the_image_rather_than_assuming_it(
        self, pipeline, make_invoice, image_template
    ):
        """The trial is where "too big to send as an image" should surface."""
        pipeline.settings.attachment_format = IMAGE
        pipeline.templates.put(image_template)
        pipeline.settings.default_template = "receipt_image"
        pipeline.settings.template_mappings = {
            "receipt_image": {"1": "customer_name", "2": "invoice_number"}
        }

        job = pipeline.process(make_invoice())

        assert job.status is JobStatus.DRY_RUN
        entry = json.loads(pipeline.sender.log_path.read_text().splitlines()[-1])
        assert entry["attachment"] == IMAGE
        assert entry["filename"].endswith(".jpg")
        assert entry["image_bytes"] > 1000


class TestAMismatchIsAHoldAndNotAFailedSend:
    """Meta settles a template's header when it approves it, so this is the
    same on every print: the document waits for a person to fix setup."""

    @pytest.fixture
    def image_install(self, pipeline, templates):
        pipeline.settings.dry_run = False
        pipeline.settings.attachment_format = IMAGE
        return pipeline

    def test_a_document_header_template_holds_the_print(
        self, image_install, make_invoice
    ):
        job = image_install.process(make_invoice())

        assert job.status is JobStatus.HELD
        assert "an image cannot be attached" in job.hold_reason
        assert "Setup → Fill in messages" in job.hold_reason

    def test_releasing_it_from_the_queue_refuses_for_the_same_reason(
        self, image_install, make_invoice
    ):
        job = image_install.process(make_invoice())

        with pytest.raises(ValueError, match="an image cannot be attached"):
            image_install.release(job.id, "+919876543210")

    def test_an_image_header_template_is_not_held(
        self, image_install, make_invoice, image_template
    ):
        image_install.templates.put(image_template)
        image_install.settings.default_template = "receipt_image"
        image_install.settings.template_mappings = {
            "receipt_image": {"1": "customer_name", "2": "invoice_number"}
        }

        job = image_install.process(make_invoice())

        assert job.status is not JobStatus.HELD

    def test_link_mode_is_left_alone(self, link_pipeline, make_invoice):
        """Nothing is attached on that route -- a person attaches the file."""
        link_pipeline.settings.attachment_format = IMAGE

        job = link_pipeline.process(make_invoice())

        assert job.status is JobStatus.READY


class TestSetupSaysItBeforeAnythingIsPrinted:
    def test_the_status_page_names_the_mismatch(self, settings, templates):
        from waprinter.send.readiness import problems

        settings.attachment_format = IMAGE

        found = problems(settings, templates)

        assert any("has nowhere to go" in p for p in found)

    def test_that_problem_points_at_the_messages_page(self):
        from waprinter.ui.viewmodel import fixed_by

        assert fixed_by(
            "Message 'invoice_document' has a document header, so the image "
            "has nowhere to go."
        ) == "messages"

    def test_the_messages_screen_warns_about_the_chosen_pairing(
        self, image_template
    ):
        from waprinter.ui import setupmodel as sm

        assert sm.template_problem(image_template, "api", IMAGE) == ""
        assert "PDF has nowhere to go" in sm.template_problem(image_template, "api", PDF)
        # Link mode attaches nothing, so neither half of this applies.
        assert sm.template_problem(image_template, "link", PDF) == ""
