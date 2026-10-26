"""自然续聊响应边界与发送失败状态的定向回归。"""
import asyncio
from collections import deque
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PLUGIN_ROOT.parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))
sys.path.insert(0, str(SRC_ROOT))


def _module(name, *, package=False):
    module = types.ModuleType(name)
    if package:
        module.__path__ = []
    sys.modules[name] = module
    return module


def _install_astrbot_stubs():
    astrbot = _module("astrbot", package=True)
    astrbot.__path__ = [str(SRC_ROOT / "astrbot")]
    api = _module("astrbot.api", package=True)
    api.logger = __import__("logging").getLogger("natural-followup-test")
    api.__path__ = []

    class Filter:
        class PlatformAdapterType:
            ALL = object()

        @staticmethod
        def _decorator(*args, **kwargs):
            if len(args) == 1 and callable(args[0]) and not kwargs:
                return args[0]
            return lambda func: func

        platform_adapter_type = _decorator
        on_llm_request = _decorator
        on_llm_response = _decorator
        on_agent_done = _decorator
        on_decorating_result = _decorator
        after_message_sent = _decorator

    event_module = _module("astrbot.api.event")
    event_module.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_module.filter = Filter

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Image:
        pass

    class Nodes:
        pass

    class At:
        def __init__(self, qq=""):
            self.qq = qq

    class AtAll:
        pass

    class Reply:
        def __init__(self, sender_id=""):
            self.sender_id = sender_id

    components = _module("astrbot.api.message_components")
    components.Plain, components.Image = Plain, Image
    components.Nodes = Nodes
    components.At, components.AtAll, components.Reply = At, AtAll, Reply

    star = _module("astrbot.api.star")
    star.Context = type("Context", (), {})
    star.Star = type("Star", (), {})
    star.register = lambda *args, **kwargs: lambda cls: cls

    core = _module("astrbot.core", package=True)
    core.__path__ = [str(SRC_ROOT / "astrbot" / "core")]
    agent = _module("astrbot.core.agent", package=True)
    agent.__path__ = []
    message = _module("astrbot.core.agent.message")
    message.TextPart = type("TextPart", (), {})
    star_core = _module("astrbot.core.star", package=True)
    star_core.__path__ = []
    filters = _module("astrbot.core.star.filter", package=True)
    filters.__path__ = []
    command = _module("astrbot.core.star.filter.command")
    command.CommandFilter = type("CommandFilter", (), {})
    command_group = _module("astrbot.core.star.filter.command_group")
    command_group.CommandGroupFilter = type("CommandGroupFilter", (), {})


_install_astrbot_stubs()
from astrbot_plugin_natural_followup.main import (  # noqa: E402
    EMPTY_REPLY_FALLBACK,
    KEY,
    NaturalFollowup,
    SILENCE_MARKERS,
    _has_non_text_result,
)
from astrbot.api.message_components import At, AtAll, Image, Plain, Reply  # noqa: E402


class FakeEngine:
    def __init__(self):
        self.lanes = {}
        self.sent = []

    def stale(self, _batch):
        return False

    def sent_reply(self, batch, text):
        self.sent.append((batch, text))


class FakeEvent:
    def __init__(self, extra=None, *, message_id="m1"):
        self.extra = dict(extra or {})
        self.message_obj = SimpleNamespace(message_id=message_id)
        self.stopped = False

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def stop_event(self):
        self.stopped = True


def _plugin():
    plugin = object.__new__(NaturalFollowup)
    plugin.settings = SimpleNamespace(
        enabled=True,
        enabled_groups={"platform:GroupMessage:group"},
        debug_stats=False,
    )
    plugin.engine = FakeEngine()
    return plugin


def _batch(*, explicit=True):
    return SimpleNamespace(
        key=("platform", "bot", "group", "user"),
        raw_reply="",
        silent=False,
        explicit=explicit,
        suppressed=False,
        cancelled=False,
        delivered=False,
        original_send=None,
        run_context=None,
        items=[SimpleNamespace(message_id="m1")],
    )


class ResponseControls(unittest.IsolatedAsyncioTestCase):
    async def test_silence_markers_stay_silent_only_for_non_explicit_followups(self):
        for marker in {"<NO_REPLY>", "<NO_RESPONSE>", *SILENCE_MARKERS}:
            with self.subTest(marker=marker):
                plugin = _plugin()
                batch = _batch(explicit=False)
                event = FakeEvent({KEY: batch})
                response = SimpleNamespace(
                    role="assistant",
                    completion_text=marker,
                    tools_call_name=[],
                    tools_call_ids=[],
                    tools_call_args=[],
                    result_chain=None,
                )
                await plugin.on_response(event, response)
                self.assertTrue(batch.silent)
                self.assertEqual(batch.raw_reply, "")
                self.assertEqual(response.completion_text, "")
                self.assertTrue(event.stopped)

    async def test_explicit_marker_and_empty_output_get_local_feedback(self):
        for output in (*SILENCE_MARKERS, "\u200b\ufeff "):
            with self.subTest(output=output):
                plugin = _plugin()
                batch = _batch(explicit=True)
                event = FakeEvent({KEY: batch})
                response = SimpleNamespace(
                    role="assistant",
                    completion_text=output,
                    tools_call_name=[],
                    tools_call_ids=[],
                    tools_call_args=[],
                    result_chain=SimpleNamespace(
                        chain=[At("bot"), Reply("bot")]
                    ),
                )
                await plugin.on_response(event, response)
                self.assertEqual(response.completion_text, EMPTY_REPLY_FALLBACK)
                self.assertEqual(batch.raw_reply, EMPTY_REPLY_FALLBACK)
                self.assertFalse(batch.silent)
                self.assertFalse(event.stopped)
                self.assertIsNone(response.result_chain)

    async def test_header_only_chain_does_not_count_as_media(self):
        headers = SimpleNamespace(chain=[At("bot"), AtAll(), Reply("bot")])
        media = SimpleNamespace(chain=[Image()])
        self.assertFalse(_has_non_text_result(headers))
        self.assertTrue(_has_non_text_result(media))

    async def test_empty_tool_response_is_not_replaced(self):
        plugin = _plugin()
        batch = _batch(explicit=True)
        event = FakeEvent({KEY: batch})
        response = SimpleNamespace(
            role="assistant",
            completion_text="",
            tools_call_name=["search"],
            tools_call_ids=["call-1"],
            tools_call_args=[{}],
            result_chain=None,
        )
        await plugin.on_response(event, response)
        self.assertEqual(response.completion_text, "")
        self.assertFalse(batch.silent)
        self.assertFalse(event.stopped)

    async def test_duplicate_after_submit_race_is_suppressed_but_capacity_falls_through(self):
        class SubmitEngine(FakeEngine):
            def candidate(self, *args, **kwargs):
                if duplicate_case[0]:
                    key = args[0]
                    self.lanes[key] = SimpleNamespace(
                        recent_ids=deque(["m1"])
                    )
                return "explicit"

            def submit(self, *args, **kwargs):
                return None, False

        class Event(FakeEvent):
            def __init__(self, message_id="m1"):
                super().__init__(message_id=message_id)
                self.message_obj = SimpleNamespace(message_id=message_id)
                self.call_llm = False
                self._has_send_oper = False
                self.is_at_or_wake_command = False
                self.unified_msg_origin = "origin"
                self.should_call = []

            def get_platform_name(self): return "aiocqhttp"
            def get_group_id(self): return "group"
            def get_platform_id(self): return "platform"
            def get_self_id(self): return "bot"
            def get_sender_id(self): return "user"
            def is_stopped(self): return False
            def get_messages(self): return []
            def get_message_str(self): return "@bot please"
            def should_call_llm(self, value): self.should_call.append(value)

        class Context:
            def __init__(self, before_submit):
                self.conversation_manager = SimpleNamespace(
                    get_curr_conversation_id=before_submit
                )

            def get_config(self, **kwargs):
                return {"agent_runner": {"runner_type": "local"}}

        async def exercise(duplicate):
            plugin = _plugin()
            plugin.engine = SubmitEngine()
            event = Event()
            duplicate_case[0] = duplicate

            plugin.context = Context(lambda _origin: asyncio.sleep(0, result=None))
            await plugin.on_message(event)
            return event

        duplicate_case = [False]
        duplicate_event = await exercise(True)
        capacity_event = await exercise(False)
        self.assertEqual(duplicate_event.should_call, [True])
        self.assertEqual(capacity_event.should_call, [False])

    async def test_delivery_failure_notice_does_not_renew_followup_state(self):
        plugin = _plugin()
        batch = _batch(explicit=True)
        assistant_message = SimpleNamespace(role="assistant", tool_calls=None)
        batch.raw_reply = "generated answer"
        batch.delivered = True
        batch.run_context = SimpleNamespace(messages=[assistant_message])
        event = FakeEvent({KEY: batch, "_respond_delivery_failed": True})
        event.get_platform_name = lambda: "aiocqhttp"
        event.get_group_id = lambda: "group"
        event.get_platform_id = lambda: "platform"
        event.get_self_id = lambda: "bot"
        event.get_result = lambda: None
        await plugin.after_sent(event)
        self.assertEqual(plugin.engine.sent, [])
        self.assertTrue(assistant_message._no_save)

    async def test_send_time_supersession_marks_delivery_suppressed(self):
        plugin = _plugin()
        batch = _batch(explicit=True)
        batch.run_context = SimpleNamespace(messages=[])
        event = FakeEvent({KEY: batch})
        sent = []
        async def send(chain):sent.append(chain)
        event.send = send
        await plugin.on_result(event)
        plugin.engine.stale = lambda batch: True
        await event.send(SimpleNamespace(get_plain_text=lambda: 'stale'))
        self.assertEqual(sent, [])
        self.assertTrue(event.get_extra('_respond_delivery_suppressed'))
        self.assertFalse(batch.delivered)

    async def test_failure_notice_through_send_wrapper_is_not_marked_delivered(self):
        plugin = _plugin()
        batch = _batch(explicit=True)
        batch.run_context = SimpleNamespace(messages=[])
        event = FakeEvent({KEY: batch})
        original = SimpleNamespace(fail=True)

        async def send(_chain):
            if original.fail:
                raise RuntimeError("simulated send failure")

        event.send = send
        await plugin.on_result(event)
        with self.assertRaisesRegex(RuntimeError, "simulated send failure"):
            await event.send(SimpleNamespace(get_plain_text=lambda: "answer"))
        original.fail = False
        event.set_extra("_respond_delivery_failed", True)
        await event.send(SimpleNamespace(get_plain_text=lambda: "failure notice"))
        self.assertFalse(batch.delivered)
        self.assertFalse(event.get_extra("_natural_followup_delivered", False))


if __name__ == "__main__":
    unittest.main()
