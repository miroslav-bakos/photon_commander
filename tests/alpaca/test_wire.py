"""The transport envelope: parameters, transaction ids, error shape."""

from __future__ import annotations

import pytest

from ssc.alpaca.errors import (
    AlpacaError,
    AlpacaNotImplementedError,
    BadRequestError,
    ErrorNumber,
)
from ssc.alpaca.wire import (
    MAX_TRANSACTION_ID,
    RequestParams,
    TransactionCounter,
    error_response,
    method_response,
    value_response,
)


def test_parameter_names_are_case_insensitive() -> None:
    params = RequestParams([("RightAscension", "5.5"), ("CLIENTTRANSACTIONID", "42")])
    assert params.as_float("rightascension") == 5.5
    assert params.as_float("RIGHTASCENSION") == 5.5
    assert params.client_transaction_id == 42


def test_first_spelling_of_a_repeated_name_wins() -> None:
    params = RequestParams([("Axis", "0"), ("axis", "2")])
    assert params.as_int("Axis") == 0


@pytest.mark.parametrize("text", ["True", "true", "TRUE", "1", "yes"])
def test_boolean_true_spellings(text: str) -> None:
    assert RequestParams([("Connected", text)]).as_bool("Connected") is True


@pytest.mark.parametrize("text", ["False", "false", "0", "no"])
def test_boolean_false_spellings(text: str) -> None:
    assert RequestParams([("Connected", text)]).as_bool("Connected") is False


def test_a_non_boolean_is_a_malformed_request() -> None:
    with pytest.raises(BadRequestError):
        RequestParams([("Connected", "maybe")]).as_bool("Connected")


def test_a_missing_parameter_is_a_malformed_request() -> None:
    with pytest.raises(BadRequestError):
        RequestParams([]).require("Action")


@pytest.mark.parametrize("supplied", [None, "", "abc", "-1", "4294967296", "1.5"])
def test_a_malformed_client_transaction_id_is_reported_as_not_supplied(
    supplied: str | None,
) -> None:
    pairs = [] if supplied is None else [("ClientTransactionID", supplied)]
    assert RequestParams(pairs).client_transaction_id == 0


def test_server_transaction_ids_increase_and_never_reach_zero() -> None:
    counter = TransactionCounter()
    issued = [counter.allocate() for _ in range(5)]
    assert issued == [1, 2, 3, 4, 5]

    wrapping = TransactionCounter(start=MAX_TRANSACTION_ID - 1)
    assert wrapping.allocate() == MAX_TRANSACTION_ID
    assert wrapping.allocate() == 1


def test_value_and_method_responses_differ_only_by_the_value_key() -> None:
    value = value_response(7, client_transaction_id=3, server_transaction_id=9)
    method = method_response(client_transaction_id=3, server_transaction_id=9)
    assert value["Value"] == 7
    assert "Value" not in method
    for body in (value, method):
        assert body["ClientTransactionID"] == 3
        assert body["ServerTransactionID"] == 9
        assert body["ErrorNumber"] == 0
        assert body["ErrorMessage"] == ""


def test_an_error_response_carries_a_number_and_a_message_and_no_value() -> None:
    body = error_response(
        AlpacaNotImplementedError("Telescope.RightAscension is not available: no method exists"),
        client_transaction_id=11,
        server_transaction_id=12,
    )
    assert body["ErrorNumber"] == ErrorNumber.NOT_IMPLEMENTED
    assert "no method exists" in str(body["ErrorMessage"])
    assert "Value" not in body


def test_an_alpaca_error_refuses_an_empty_message() -> None:
    with pytest.raises(ValueError, match="non-empty ErrorMessage"):
        AlpacaError("   ")
