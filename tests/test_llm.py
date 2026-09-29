import json
import unittest

from analytics_agent.llm import Completion, OpenAICompatibleProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class LLMProviderTests(unittest.TestCase):
    def test_posts_openai_compatible_chat_request_without_leaking_key(self):
        seen = {}

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            seen["timeout"] = timeout
            seen["body"] = json.loads(request.data)
            return FakeResponse({
                "choices": [{"message": {"content": "{\"sql\":\"SELECT 1\",\"needs_clarification\":false}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            })

        provider = OpenAICompatibleProvider(
            base_url="https://llm.example/v1",
            model="model-test",
            api_key="do-not-print-this",
            opener=opener,
        )
        result = provider.complete(({"role": "user", "content": "test"},))
        self.assertEqual("https://llm.example/v1/chat/completions", seen["url"])
        self.assertEqual("Bearer do-not-print-this", seen["auth"])
        self.assertEqual("model-test", seen["body"]["model"])
        self.assertEqual(14, result.usage["total_tokens"])

    def test_parses_next_action_and_includes_user_context(self):
        class StubProvider(OpenAICompatibleProvider):
            def complete(self, messages, *, max_tokens=1200):
                self.messages = messages
                return type("Completion", (), {
                    "text": '{"action":"run_select","sql":"SELECT 1","notes":"criterio probado"}',
                    "usage": {"total_tokens": 9},
                })()

        provider = StubProvider(base_url="https://llm.example/v1", model="m", api_key="k")
        result, usage = provider.next_action(
            "suma primas",
            context="usa todos los movimientos",
            last_tool_result={"action": "search_tables", "tables": []},
        )
        self.assertEqual("run_select", result["action"])
        self.assertEqual("SELECT 1", result["sql"])
        self.assertEqual(9, usage["total_tokens"])
        self.assertIn("usa todos los movimientos", provider.messages[1]["content"])
        self.assertIn("search_tables", provider.messages[1]["content"])

    def test_responses_api_keeps_storage_disabled(self):
        seen = {}

        def opener(request, timeout):
            seen["body"] = json.loads(request.data)
            return FakeResponse({
                "output_text": '{"action":"finish","answer":"Listo"}',
                "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            })

        provider = OpenAICompatibleProvider(
            base_url="https://llm.example/v1",
            model="model-test",
            api_key="key",
            wire_api="responses",
            store_responses=False,
            opener=opener,
        )
        provider.complete(({"role": "user", "content": "test"},))
        self.assertFalse(seen["body"]["store"])

    def test_parses_analysis_plan_and_passes_memory_and_glossary(self):
        class StubProvider(OpenAICompatibleProvider):
            def complete(self, messages, *, max_tokens=1200):
                self.messages = messages
                return Completion(
                    text=json.dumps({
                        "action": "plan",
                        "plan": {"metric": "prima", "period": "2026-S1", "grain": "ramo"},
                    }),
                    usage={"total_tokens": 9},
                )

        provider = StubProvider(base_url="https://llm.example/v1", model="m", api_key="k")
        result, _ = provider.next_action(
            "prima por ramo",
            retrieved_memory="Cuenta todos los movimientos.",
            glossary_context='[{"term":"prima emitida"}]',
        )
        self.assertEqual("plan", result["action"])
        self.assertEqual("ramo", result["plan"]["grain"])
        self.assertIn("Cuenta todos los movimientos", provider.messages[1]["content"])
        self.assertIn("prima emitida", provider.messages[1]["content"])

    def test_reviewer_returns_structured_decision(self):
        class StubProvider(OpenAICompatibleProvider):
            def complete(self, messages, *, max_tokens=1200):
                self.messages = messages
                return Completion(
                    text='{"accepted":true,"issues":[]}',
                    usage={"total_tokens": 5},
                )

        provider = StubProvider(base_url="https://llm.example/v1", model="m", api_key="k")
        result, usage = provider.review_answer(
            "prima por ramo",
            plan={"metric": "prima", "grain": "ramo"},
            draft={"answer": "La suma fue 10."},
            action_history=[{"action": "run_select"}],
            evidence=[{"rows": [{"total": 10}]}],
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(5, usage["total_tokens"])
        self.assertIn("run_select", provider.messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
