"""Execute the production PHP validator with mail replaced by a local test sink."""
import json
import os
import shutil
import subprocess
from email import policy
from email.parser import Parser
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from ssc.api.diagnostics import BugReport, DiagnosticEvent


@pytest.fixture
def receiver(tmp_path):
    php = os.environ.get('SSC_TEST_PHP') or shutil.which('php')
    if not php:
        pytest.skip('PHP runtime not installed; set SSC_TEST_PHP for receiver validation')
    source = Path('packaging/report-receiver/report.php').read_text()
    harness = '''
namespace PhotonReceiverTest;
function sys_get_temp_dir(): string { return __DIR__; }
function mail($to, $subject, $body, $headers, $envelope = ""): bool {
    file_put_contents(__DIR__ . '/mail.jsonl', json_encode([$to, $subject, $body, $headers, $envelope]) . "\\n", FILE_APPEND);
    return true;
}
$_SERVER['REQUEST_METHOD'] = 'POST';
$_SERVER['CONTENT_TYPE'] = 'application/json';
'''
    source = source.replace('declare(strict_types=1);', 'declare(strict_types=1);' + harness)
    source = source.replace("'php://input'", "'php://stdin'")
    script = tmp_path / 'receiver.php'
    script.write_text(source)

    def invoke(payload):
        result = subprocess.run([php, '-n', str(script)], input=json.dumps(payload),
                                text=True, capture_output=True, check=True)
        assert not result.stderr
        return json.loads(result.stdout)
    return invoke, tmp_path / 'mail.jsonl'


def report():
    return BugReport(report_id='c' * 32, build='a' * 12, category='other', logging_enabled=True,
                     events=[DiagnosticEvent(event='b' * 16, level='DEBUG', elapsed_ms=1,
                                             exception=False, trace=[])]).model_dump()


def test_receiver_validates_and_deduplicates_without_real_mail(receiver):
    invoke, mail = receiver
    payload = report()
    assert invoke(payload) == {'report_id': payload['report_id']}
    assert invoke(payload) == {'report_id': payload['report_id']}
    deliveries = mail.read_text().splitlines()
    assert len(deliveries) == 1
    to, _, body, headers, envelope = json.loads(deliveries[0])
    assert to == 'miroslav@miroslavbakos.com'
    assert envelope == '-fmiroslav@miroslavbakos.com'
    message = Parser(policy=policy.default).parsestr(headers + '\r\n\r\n' + body)
    attachment, = list(message.iter_attachments())
    assert attachment.get_content_type() == 'application/zip'
    with ZipFile(BytesIO(attachment.get_payload(decode=True))) as archive:
        name, = archive.namelist()
        assert name == f"photon-report-{payload['report_id']}.json"
        assert json.loads(archive.read(name)) == payload


@pytest.mark.parametrize('boundary', ['report', 'event', 'trace'])
def test_receiver_rejects_private_fields_without_mail(receiver, boundary):
    invoke, mail = receiver
    payload = report()
    if boundary == 'report':
        payload['location'] = 'private-location'
    elif boundary == 'event':
        payload['events'][0]['message'] = 'private-location'
    else:
        payload['events'][0]['trace'] = ['private-location']
    assert 'error' in invoke(payload)
    assert not mail.exists()


def test_receiver_session_quota_does_not_block_other_sessions(receiver):
    invoke, mail = receiver
    for index in range(10):
        payload = report()
        payload['report_id'] = f'{index:032x}'
        payload['events'][0]['session'] = 'a' * 16
        assert invoke(payload) == {'report_id': payload['report_id']}
    payload['report_id'] = 'f' * 32
    assert 'error' in invoke(payload)
    payload['events'][0]['session'] = 'b' * 16
    assert invoke(payload) == {'report_id': payload['report_id']}
    assert len(mail.read_text().splitlines()) == 11


def test_receiver_rejects_empty_evidence_without_mail(receiver):
    invoke, mail = receiver
    payload = report()
    payload['events'] = []
    assert 'error' in invoke(payload)
    assert not mail.exists()


def test_receiver_receipt_storage_is_bounded_without_blocking_delivery(receiver):
    invoke, mail = receiver
    directory = mail.parent / 'photon-report-receipts'
    directory.mkdir()
    for index in range(1000):
        (directory / f'{index:032x}.sent').write_text('a' * 16)
    payload = report()
    payload['events'][0]['session'] = 'b' * 16
    assert invoke(payload) == {'report_id': payload['report_id']}
    assert len(list(directory.glob('*.sent'))) == 1000
