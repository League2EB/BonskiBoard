from app.feishu import is_successful_submit_response


def test_only_explicit_feishu_success_code_is_accepted() -> None:
    assert is_successful_submit_response({"code": 0, "data": {"canSubmitAgain": True}})
    assert not is_successful_submit_response({"code": 1})
    assert not is_successful_submit_response({"data": {"code": 0}})
    assert not is_successful_submit_response([])
    assert not is_successful_submit_response(None)
