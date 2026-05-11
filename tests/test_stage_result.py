from src.pipeline import StageResult


class TestStageResult:
    def test_ok(self):
        result = StageResult.ok("hello")
        assert result.success is True
        assert result.data == "hello"
        assert result.error is None
        assert result.failed is False

    def test_ok_with_none_data(self):
        result = StageResult.ok(None)
        assert result.success is True
        assert result.data is None

    def test_fail(self):
        result = StageResult.fail("something went wrong")
        assert result.success is False
        assert result.data is None
        assert result.error == "something went wrong"
        assert result.failed is True

    def test_fail_with_partial_data(self):
        result = StageResult.fail("partial failure", data={"status": "scraped"})
        assert result.success is False
        assert result.error == "partial failure"
        assert result.data == {"status": "scraped"}
        assert result.failed is True

    def test_generic_with_int(self):
        result = StageResult[int].ok(42)
        assert result.data == 42

    def test_generic_with_dict(self):
        result = StageResult[dict].ok({"key": "value"})
        assert result.data == {"key": "value"}

    def test_failed_property(self):
        ok_result = StageResult.ok("data")
        fail_result = StageResult.fail("err")
        assert ok_result.failed is False
        assert fail_result.failed is True
