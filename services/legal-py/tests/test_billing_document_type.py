"""Finance reads the registered document type with the exact version."""
import pytest
from helpers import Harness


@pytest.mark.parametrize('doc_id,expected', [('client_msa', 'client_msa'), ('sow', 'sow'),
                                           ('sow_seo_audit', 'sow'), ('sow_revenue_recovery', 'sow')])
def test_version_read_exposes_registered_document_type(doc_id, expected):
    h = Harness()
    h.approve_rules()
    h.upload(doc_id, 'Contract text for the document-type read.', '1.0', 'zbm')
    response = h.ok(h.get(f'/legal/v1/documents/{doc_id}/versions/1.0', caller='finance_31'))
    assert response.get('doc_type') == expected
    assert response['doc_id'] == doc_id and response['version'] == '1.0'
