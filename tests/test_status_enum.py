"""``Status`` enum — values must serialize as JSON strings and compare with
literals so the Jinja templates that reference ``"done"`` etc. keep working."""

from c24_client import Status


def test_values_are_the_expected_strings():
    assert Status.AWAITING_CODE.value == "awaiting_code"
    assert Status.LOGGING_IN.value == "logging_in"
    assert Status.DOWNLOADING.value == "downloading"
    assert Status.DONE.value == "done"
    assert Status.LOGIN_FAILED.value == "login_failed"
    assert Status.DOWNLOAD_FAILED.value == "download_failed"


def test_compares_equal_to_string_literal():
    # Templates and JSON consumers compare with literals; we need this to hold.
    assert Status.DONE == "done"
    assert Status.AWAITING_CODE == "awaiting_code"


def test_str_round_trip_for_json_payloads():
    # Flask's jsonify serializes str-Enum to its string value.
    import json
    assert json.dumps(Status.DOWNLOADING.value) == '"downloading"'
