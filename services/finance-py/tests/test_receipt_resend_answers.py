import pytest
import answers
from ports import ReceiptDeliveryOutcome


@pytest.mark.parametrize('result',[ReceiptDeliveryOutcome('accepted','bad id'), ReceiptDeliveryOutcome('accepted',True),
    ReceiptDeliveryOutcome('uncertain','owned-id'),ReceiptDeliveryOutcome('failed','owned-id'),
    ReceiptDeliveryOutcome('retry','owned-id')])
def test_provider_identity_shape_and_outcome_relationship(result):
    with pytest.raises(answers.Malformed):
        answers.check(result,ReceiptDeliveryOutcome('uncertain'),10**12)


@pytest.mark.parametrize('outcome',['accepted','retry','failed','uncertain'])
def test_existing_mock_contract_kept(outcome):
    result=ReceiptDeliveryOutcome(outcome)
    assert answers.check(result,ReceiptDeliveryOutcome('uncertain'),10**12)==result
