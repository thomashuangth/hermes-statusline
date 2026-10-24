"""Tests for transcript persistence isolation (zero extra LLM tokens, same-turn exact suffix stripping)."""

import copy
import unittest

from _helpers import sl


class TestPersistenceIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_exact_same_turn_suffix_stripped_from_response(self):
        seen_kwargs = []

        async def fake_persist(runner, **kwargs):
            seen_kwargs.append(kwargs)
            return "persisted_ok"

        footer = "`gpt-6.1-sol · low · ctx 24% · 5h 18% (18:40) · 7j 39% (31/10 09:00)`"
        raw_body = "This is the final assistant response to the user."
        delivered_response = f"{raw_body}\n\n{footer}"

        agent_result = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "This is the final assistant response to the user."},
            ],
            "last_prompt_tokens": 1200,
            "context_length": 100_000,
        }
        original_agent_result = copy.deepcopy(agent_result)

        kwargs = {
            "response": delivered_response,
            "agent_result": agent_result,
            "session_key": "telegram:chat_1:thread_1",
        }

        res = await sl.persist_without_footer(fake_persist, None, kwargs, footer)

        self.assertEqual(res, "persisted_ok")
        self.assertEqual(len(seen_kwargs), 1)

        # The response persisted to transcript must have the statusline footer stripped
        self.assertEqual(seen_kwargs[0]["response"], raw_body)

        # The original agent_result dictionary and messages list must be untouched
        self.assertEqual(agent_result, original_agent_result)
        self.assertEqual(seen_kwargs[0]["agent_result"], original_agent_result)

    async def test_response_without_footer_is_unmodified(self):
        seen_kwargs = []

        async def fake_persist(runner, **kwargs):
            seen_kwargs.append(kwargs)
            return "ok"

        body = "Response with no footer attached."
        kwargs = {"response": body, "agent_result": {"messages": []}}

        await sl.persist_without_footer(fake_persist, None, kwargs, "`some-footer`")
        self.assertEqual(seen_kwargs[0]["response"], body)

    async def test_unrelated_backticks_not_stripped(self):
        seen_kwargs = []

        async def fake_persist(runner, **kwargs):
            seen_kwargs.append(kwargs)
            return "ok"

        body = "Check this code: `print('hello')`"
        kwargs = {"response": body, "agent_result": {"messages": []}}

        await sl.persist_without_footer(fake_persist, None, kwargs, "`other · footer`")
        self.assertEqual(seen_kwargs[0]["response"], body)


if __name__ == "__main__":
    unittest.main()
