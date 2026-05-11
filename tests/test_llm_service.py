from src.llm.service import LLMService


class TestLLMServiceParseJSON:
    def test_parse_valid_json(self):
        raw = '{"key": "value", "num": 42}'
        result = LLMService._parse_json(raw)
        assert result == {"key": "value", "num": 42}
        assert "_parse_error" not in result

    def test_parse_json_with_markdown_fence(self):
        raw = '```json\n{"key": "value"}\n```'
        result = LLMService._parse_json(raw)
        assert result == {"key": "value"}
        assert "_parse_error" not in result

    def test_parse_json_with_generic_fence(self):
        raw = '```\n{"key": "value"}\n```'
        result = LLMService._parse_json(raw)
        assert result == {"key": "value"}
        assert "_parse_error" not in result

    def test_parse_json_with_extra_whitespace(self):
        raw = '  \n  {"key": "value"}  \n  '
        result = LLMService._parse_json(raw)
        assert result == {"key": "value"}

    def test_parse_nested_json(self):
        raw = '{"outer": {"inner": [1, 2, 3]}, "flag": true}'
        result = LLMService._parse_json(raw)
        assert result == {"outer": {"inner": [1, 2, 3]}, "flag": True}

    def test_parse_invalid_json(self):
        raw = "not json at all"
        result = LLMService._parse_json(raw)
        assert result.get("_parse_error") is True
        assert result.get("_raw") == "not json at all"

    def test_parse_malformed_json(self):
        raw = '{"key": value}'
        result = LLMService._parse_json(raw)
        assert result.get("_parse_error") is True

    def test_parse_empty_string(self):
        result = LLMService._parse_json("")
        assert result.get("_parse_error") is True

    def test_parse_fenced_invalid_json(self):
        raw = '```json\nTHIS IS NOT JSON!!!\n```'
        result = LLMService._parse_json(raw)
        assert result.get("_parse_error") is True

    def test_parse_single_markdown_fence(self):
        raw = '```json\n{"answer": 42}\n```'
        result = LLMService._parse_json(raw)
        assert result == {"answer": 42}

    def test_parse_empty_object(self):
        result = LLMService._parse_json("{}")
        assert result == {}

    def test_parse_empty_array(self):
        result = LLMService._parse_json("[]")
        assert result == []


class TestLLMServiceInit:
    def test_init_basic(self):
        llm = LLMService(api_key="test-key")
        assert llm.model_text == "deepseek-chat"
        assert llm.model_vision == "deepseek-chat"
        assert llm.temperature == 0.3

    def test_init_custom(self):
        llm = LLMService(
            api_key="test-key",
            base_url="https://custom.api.com",
            model_text="gpt-4",
            model_vision="gpt-4-vision",
            temperature=0.7,
        )
        assert llm.model_text == "gpt-4"
        assert llm.model_vision == "gpt-4-vision"
        assert llm.temperature == 0.7
