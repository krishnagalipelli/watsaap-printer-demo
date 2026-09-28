"""Regression examples for wrong-template sends and untested teaching rules."""
from dataclasses import replace
import importlib.util
from pathlib import Path

import fitz
import pytest

from waprinter import validation
from waprinter.config import Settings, paths
from waprinter.extract import extract_fields
from waprinter.extract.profile import DocumentKind, DocumentProfile, FieldRule
from waprinter.models import JobStatus
from test_teaching import statement


def pdf(path, lines):
    with fitz.open() as doc:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((55, 65 + i * 28), line, fontsize=12)
        doc.save(path)
    return path


@pytest.mark.parametrize('title,body,kind', [
    ('REMOVAL LETTER', 'This follows the removal notice issued earlier.', 'removal_letter'),
    ('REMOVAL NOTICE', 'Payment received from the subscriber.', 'removal_notice'),
])
def test_heading_beats_incidental_body_phrases(tmp_path, title, body, kind):
    fields = extract_fields(pdf(tmp_path / 'p.pdf', [title, body, 'Mobile: 9876543210']))
    assert fields.document_kind == kind


def test_conflicting_headings_hold_even_in_confirmation_mode(link_pipeline, tmp_path):
    link_pipeline.settings.confirm_before_send = True
    document = pdf(tmp_path / 'mixed.pdf', ['REMOVAL LETTER', 'REMOVAL NOTICE', 'Mobile: 9876543210'])
    job = link_pipeline.process(document)
    assert job.status == JobStatus.HELD
    assert 'Conflicting' in job.hold_reason
    assert job.template_name is None
    assert link_pipeline.store.get(job.id).fields.classification_error
    with pytest.raises(ValueError, match='Choose the document type'):
        link_pipeline.release(job.id, '+919876543210')


def test_unknown_native_document_cannot_use_default_message(link_pipeline, tmp_path):
    job = link_pipeline.process(pdf(tmp_path / 'other.pdf', [
        'ACCOUNT SUMMARY', 'Mobile: 9876543210', 'Total: 150.00']))
    assert job.status == JobStatus.HELD
    assert job.template_name is None
    with pytest.raises(ValueError, match='document type'):
        link_pipeline.release(job.id, '+919876543210')


def test_manual_type_selection_rereads_fields_with_correct_anchors(link_pipeline, tmp_path):
    job = link_pipeline.process(pdf(tmp_path / 'other.pdf', [
        'UNREADABLE HEADING', 'To, RAVI KUMAR', 'Mobile: 9876543210',
        'Receipt No: RN-731', 'Date: 12/05/2026']))
    chosen = link_pipeline.fields_for_kind(job.pdf_path, 'removal_notice')
    assert chosen.document_kind == 'removal_notice'
    assert chosen.customer_name == 'RAVI KUMAR'
    assert not chosen.classification_error


def test_body_only_payment_wording_is_not_receipt_evidence(tmp_path):
    fields = extract_fields(pdf(tmp_path / 'p.pdf', ['BALANCE STATEMENT', 'Payment received from a member.']))
    assert fields.document_kind is None


@pytest.fixture
def sample_set(tmp_path):
    profile = DocumentProfile(custom_kinds=[DocumentKind('statement', ['MEMBER STATEMENT'])],
        field_rules={'statement': [FieldRule('customer_id', 'Cust ID', 'code', r'(SCF-\d+)')]})
    samples = [validation.Sample(statement(tmp_path / f'{i}.pdf', cust_id=f'SCF-{i}'),
                'statement', {'customer_id': f'SCF-{i}'}, '+919000012345') for i in range(3)]
    return profile, samples


def test_three_independently_reviewed_samples_pass_without_jobs(sample_set):
    profile, samples = sample_set
    report = validation.validate(samples, profile, Settings(), target='statement')
    assert report.passed, report.summary()
    assert not paths().profile.exists()
    assert not (paths().root / "jobs.db").exists()


def test_same_pdf_repeated_does_not_count_as_multiple_samples(sample_set):
    profile, samples = sample_set
    report = validation.validate([samples[0]] * 3, profile, Settings(), target='statement')
    assert not report.passed
    assert '1 provided' in report.summary()


def test_wrong_values_and_wrong_mobile_are_not_accepted(sample_set):
    profile, samples = sample_set
    samples[1] = replace(samples[1], values={'customer_id': 'RN-99'}, recipient='+919876543210')
    report = validation.validate(samples, profile, Settings(), target='statement')
    assert not report.passed
    assert 'RN-99' in report.summary() and 'Recipient:' in report.summary()


def test_sample_of_another_kind_detects_collision(sample_set, tmp_path):
    profile, samples = sample_set
    other = pdf(tmp_path / 'notice.pdf', ['MEMBER STATEMENT', 'REMOVAL NOTICE', 'Mobile: 9876543210'])
    samples.append(validation.Sample(other, 'removal_notice', recipient='+919876543210'))
    report = validation.validate(samples, profile, Settings(), target='statement')
    assert not report.passed
    assert 'Conflicting' in report.summary()


def test_samples_persist_with_expected_values(sample_set):
    profile, samples = sample_set
    validation.save_samples(samples)
    loaded = validation.saved_samples()
    assert len(loaded) == 3
    assert validation.validate(loaded, profile, Settings(), target='statement').passed


def test_reader_cache_invalidates_for_content_and_rules(sample_set, monkeypatch):
    profile, samples = sample_set
    original = validation.extract_fields
    calls = []
    def read(*a, **kw):
        calls.append(1)
        return original(*a, **kw)
    monkeypatch.setattr(validation, 'extract_fields', read)
    reader = validation.Reader()
    reader.read(samples[0].path, profile, Settings())
    reader.read(samples[0].path, profile, Settings())
    assert len(calls) == 1
    reader.read(samples[0].path, replace(profile, field_rules={}), Settings())
    assert len(calls) == 2
    samples[0].path.write_bytes(samples[1].path.read_bytes())
    reader.read(samples[0].path, profile, Settings())
    assert len(calls) == 3


def test_corrupt_sample_fails_validation_instead_of_being_skipped(sample_set):
    profile, samples = sample_set
    samples[2].path.write_bytes(b'broken')
    report = validation.validate(samples, profile, Settings(), target='statement')
    assert not report.passed
    assert report.results[2].errors


def test_release_manifest_requires_both_exact_version_builds(tmp_path):
    module_path = Path(__file__).parents[1] / 'packaging' / 'make_update_manifest.py'
    spec = importlib.util.spec_from_file_location('manifest_test', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / 'WhatsAppPrinter-Setup-0.1.6.exe').write_bytes(b'x64')
    with pytest.raises(ValueError, match='Missing installer'):
        module.manifest(tmp_path, 'owner/repo', 'v0.1.6')
    (tmp_path / 'WhatsAppPrinter-Setup-0.1.6-win7-x86.exe').write_bytes(b'x86')
    result = module.manifest(tmp_path, 'owner/repo', 'v0.1.6')
    assert result['url'] == result['builds']['x64']['url']
    assert result['builds']['win7-x86']['url'].endswith('-win7-x86.exe')
    assert result['builds']['win7-x86']['sha256'] != result['sha256']
    with pytest.raises(ValueError):
        module.manifest(tmp_path, 'owner/repo', 'v0.1.7')


def test_legacy_samples_still_protect_other_types(tmp_path):
    paths().samples.mkdir(parents=True, exist_ok=True)
    pdf(paths().samples / 'removal_notice.pdf', ['REMOVAL NOTICE'])
    stored = validation.saved_samples(except_kind='statement')
    assert stored[0].kind == 'removal_notice'
    assert stored[0].recipient is None
    collision = DocumentProfile(custom_kinds=[DocumentKind('statement', ['REMOVAL NOTICE'])])
    assert not validation.validate(stored, collision, Settings()).passed


def test_validation_checks_rendered_template_values(sample_set, tmp_path):
    from waprinter.send.templates import TemplateStore, MessageTemplate
    profile, samples = sample_set
    templates = TemplateStore(tmp_path / 'templates.json')
    templates.put(MessageTemplate('statement_message', body='Dear {{customer_id}}, balance {{missing_balance}}', parameter_format='named'))
    settings = Settings(document_templates={'statement': 'statement_message'})
    report = validation.validate(samples, profile, settings, target='statement', templates=templates)
    assert not report.passed
    assert 'Message values missing' in report.summary()


def test_validation_uses_ocr_settings_for_scanned_samples(sample_set, monkeypatch):
    from waprinter.models import ExtractedFields
    profile, samples = sample_set
    calls = []
    def fake_read(*a, **kw):
        calls.append(kw['ocr'])
        return ExtractedFields(has_text_layer=False, used_ocr=True, document_kind='statement')
    monkeypatch.setattr(validation, 'extract_fields', fake_read)
    report = validation.validate([replace(samples[0], values={}, recipient='')],
                                 replace(profile, field_rules={}), Settings())
    assert report.passed
    assert calls and calls[0].enabled
