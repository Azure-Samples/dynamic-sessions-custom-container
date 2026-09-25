import os
import sys
import types
import unittest
from unittest.mock import patch

os.environ.pop("AZURE_OPENAI_ENDPOINT", None)


def fake_ai_function(func=None, **_kwargs):
    def decorator(inner):
        return inner

    return decorator(func) if func else decorator


agent_framework = types.ModuleType("agent_framework")
agent_framework.ChatAgent = object
agent_framework.AgentThread = object
agent_framework.ai_function = fake_ai_function
agent_framework_azure = types.ModuleType("agent_framework.azure")
agent_framework_azure.AzureOpenAIChatClient = object
sys.modules["agent_framework"] = agent_framework
sys.modules["agent_framework.azure"] = agent_framework_azure

import main


class FakeThread:
    def __init__(self):
        self.messages = []


class FakeResult:
    def __init__(self, text):
        self.text = text


class FakeAgent:
    def get_new_thread(self):
        return FakeThread()

    async def run(self, prompt, thread):
        thread.messages.append(prompt)
        return FakeResult(f"echo: {prompt}")


class FakeAccessToken:
    token = "test-token"


class FakeCredential:
    def get_token(self, _audience):
        return FakeAccessToken()


class FakeExecutionResponse:
    status_code = 200
    headers = {}
    text = '{"output":"ok","error":"","return_code":0,"success":true}'

    def json(self):
        return {
            "output": "ok",
            "error": "",
            "return_code": 0,
            "success": True,
        }


class SessionIsolationTests(unittest.TestCase):
    def setUp(self):
        main.agent = FakeAgent()
        main.conversation_threads.clear()
        main.app.config["TESTING"] = True

    def test_caller_supplied_session_id_is_ignored(self):
        first_client = main.app.test_client()
        second_client = main.app.test_client()

        first_response = first_client.post(
            "/api/chat/",
            json={"prompt": "first", "session_id": "shared"},
        )
        second_response = second_client.post(
            "/api/chat/",
            json={"prompt": "second", "session_id": "shared"},
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(len(main.conversation_threads), 2)

    def test_response_does_not_expose_session_state(self):
        client = main.app.test_client()

        response = client.post("/api/chat/", json={"prompt": "hello"})
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("active_sessions", payload)
        self.assertNotIn("session_id", payload)
        self.assertNotIn("sensitive output", response.get_data(as_text=True))

    def test_cookie_preserves_only_the_callers_thread(self):
        client = main.app.test_client()

        client.post("/api/chat/", json={"prompt": "first"})
        client.post("/api/chat/", json={"prompt": "second"})

        self.assertEqual(len(main.conversation_threads), 1)
        thread = next(iter(main.conversation_threads.values()))["thread"]
        self.assertEqual(thread.messages, ["first", "second"])

    def test_session_cookie_is_http_only_and_secure_over_https(self):
        client = main.app.test_client()
        original_secure_setting = main.SESSION_COOKIE_SECURE
        main.SESSION_COOKIE_SECURE = True

        try:
            response = client.post("/api/chat/", json={"prompt": "hello"})
        finally:
            main.SESSION_COOKIE_SECURE = original_secure_setting
        cookie = response.headers["Set-Cookie"]

        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_forged_session_cookie_is_replaced(self):
        client = main.app.test_client()
        client.set_cookie(main.SESSION_COOKIE_NAME, "a" * 32)

        response = client.post("/api/chat/", json={"prompt": "hello"})
        cookie = response.headers["Set-Cookie"]

        self.assertNotIn("a" * 32, main.conversation_threads)
        self.assertIn(main.SESSION_COOKIE_NAME, cookie)
        self.assertEqual(len(main.conversation_threads), 1)

    def test_delete_clears_only_the_callers_session(self):
        first_client = main.app.test_client()
        second_client = main.app.test_client()

        first_client.post("/api/chat/", json={"prompt": "first"})
        first_session_id = next(iter(main.conversation_threads))
        second_client.post("/api/chat/", json={"prompt": "second"})
        second_session_id = next(
            session_id
            for session_id in main.conversation_threads
            if session_id != first_session_id
        )

        response = first_client.delete("/api/chat/session")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(first_session_id, main.conversation_threads)
        self.assertIn(second_session_id, main.conversation_threads)

    def test_conversation_threads_are_bounded(self):
        original_limit = main.MAX_CONVERSATION_THREADS
        main.MAX_CONVERSATION_THREADS = 2
        try:
            for prompt in ("first", "second", "third"):
                main.app.test_client().post("/api/chat/", json={"prompt": prompt})
        finally:
            main.MAX_CONVERSATION_THREADS = original_limit

        self.assertEqual(len(main.conversation_threads), 2)

    def test_dynamic_session_identifier_is_scoped_to_current_client(self):
        original_endpoint = main.SESSION_POOL_ENDPOINT
        main.SESSION_POOL_ENDPOINT = "https://pool.example"
        requested_urls = []
        authorization_headers = []

        def fake_post(url, **kwargs):
            requested_urls.append(url)
            authorization_headers.append(kwargs["headers"]["Authorization"])
            return FakeExecutionResponse()

        try:
            with (
                patch("azure.identity.DefaultAzureCredential", return_value=FakeCredential()),
                patch("azure.identity.ManagedIdentityCredential", return_value=FakeCredential()),
                patch.object(main.requests, "post", side_effect=fake_post),
            ):
                first_token = main.current_session_id.set("a" * 32)
                try:
                    main.execute_in_dynamic_session("print('first')")
                finally:
                    main.current_session_id.reset(first_token)

                second_token = main.current_session_id.set("b" * 32)
                try:
                    main.execute_in_dynamic_session("print('second')")
                finally:
                    main.current_session_id.reset(second_token)
        finally:
            main.SESSION_POOL_ENDPOINT = original_endpoint

        self.assertEqual(
            requested_urls,
            [
                f"https://pool.example/execute?identifier={'a' * 32}",
                f"https://pool.example/execute?identifier={'b' * 32}",
            ],
        )
        self.assertEqual(authorization_headers, ["Bearer test-token", "Bearer test-token"])


if __name__ == "__main__":
    unittest.main()
