"""在隔离的官方 AstrBot 源码与数据目录中运行；只替换外部模型和 QQ 发送端。"""
import asyncio
import contextvars
from dataclasses import replace
import json
import sys
from pathlib import Path

CASE = contextvars.ContextVar("case")
sys.path.insert(0, str(Path.cwd()))


async def main():
    from astrbot.core import astrbot_config as config, db_helper, LogBroker, logger
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember, Group
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.message.components import Plain, At, Image
    from astrbot.core.provider.entities import LLMResponse

    config["provider"] = [{"id": "fixture", "type": "openai_chat_completion", "enable": True,
                           "key": ["fictional-test-key"], "api_base": "http://127.0.0.1:9/v1",
                           "model": "fixture", "modalities": ["text", "image"]}]
    config["agent_runner"]["config"]["model"]["provider_id"] = "fixture"
    config["wake_prefix"] = ["/", "#"]
    config["admins_id"] = ["alice"]
    config["provider_settings"].update(streaming_response=False, buffer_intermediate_messages=True,
                                      show_tool_use_status=False, show_tool_call_result=False)
    config["platform_settings"]["segmented_reply"]["enable"] = False
    # 测试在几秒内发送数十条消息；原生一分钟限流另属框架职责。
    config["platform_settings"]["rate_limit"]["count"] = 1000
    config.save_config()
    core = AstrBotCoreLifecycle(LogBroker(), db_helper)
    await core.initialize()
    scheduler = core.pipeline_scheduler_mapping["default"]
    context = core.star_context
    await context.conversation_manager.new_conversation("fixture-platform:GroupMessage:test-group")
    plugin = next(s.star_cls for s in context.get_all_stars() if s.name == "astrbot_plugin_natural_followup")
    assert plugin is not None, "插件加载失败"
    provider = context.get_all_providers()[0]
    calls = []
    events = []
    tool_started = asyncio.Event()
    tool_calls = []
    from astrbot.core.agent.tool import FunctionTool

    class FixtureTool(FunctionTool):
        async def call(self, context, **kwargs):
            tool_calls.append("search")
            tool_started.set()
            await asyncio.sleep(.3)
            return "虚构搜索结果：星河上映于测试年份。"

    context.provider_manager.llm_tools.func_list.append(FixtureTool(
        name="fixture_search", description="隔离验收的搜索替身", parameters={"type": "object", "properties": {}}
    ))

    async def fake_model(*args, **kwargs):
        case = CASE.get()
        calls.append({"id": case["id"], "request": kwargs})
        case["entered"].set()
        if case.get("tool") and not case.get("tool_requested"):
            case["tool_requested"] = True
            return LLMResponse(role="assistant", completion_text="搜索前不应抢发的中间话",
                               tools_call_name=["fixture_search"], tools_call_ids=["fixture-call"], tools_call_args=[{}])
        await asyncio.sleep(case.get("delay", 0))
        if case.get("error"):
            raise RuntimeError("测试模型错误")
        return LLMResponse(role="assistant", completion_text=case.get("answer", "评价什么？"))
    provider.text_chat = fake_model

    class Event(AstrMessageEvent):
        bot = None
        async def send(self, chain):
            if self.fail:
                raise RuntimeError("测试发送失败")
            if chain and chain.get_plain_text().strip():
                self.outputs.append(chain.get_plain_text())
                self._has_send_oper = True

    def message(text, *, sender="alice", group="test-group", mention=False, other=False, image=False, fail=False, mid=None):
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.self_id = "test-bot"
        msg.session_id = group
        msg.message_id = mid or str(len(events) + 1)
        msg.group = Group(group_id=group, group_name="虚构测试群")
        msg.sender = MessageMember(sender, sender)
        msg.message_str = text
        msg.message = [Plain(text)]
        if mention:
            msg.message.insert(0, At(qq="test-bot"))
        if other:
            msg.message.insert(0, At(qq="other-user"))
        if image:
            from PIL import Image as PILImage
            path = Path(f"data/temp/test-{msg.message_id}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            PILImage.new("RGB", (24, 24), "red").save(path)
            msg.message.append(Image.fromFileSystem(str(path.resolve())))
        msg.raw_message = {}
        event = Event(text, msg, PlatformMetadata("aiocqhttp", "测试", "fixture-platform"), group)
        event.outputs, event.fail = [], fail
        if image:
            event.track_temporary_local_file(str(path.resolve()))
        events.append(event)
        return event

    async def execute(event, **options):
        case = {"id": event.message_obj.message_id, "entered": asyncio.Event(), **options}
        token = CASE.set(case)
        try:
            await scheduler.execute(event)
        finally:
            CASE.reset(token)
        return event

    async def run(event, **options):
        result = await asyncio.wait_for(asyncio.create_task(execute(event, **options)), 15)
        await asyncio.sleep(0)
        return result

    def key(sender="alice", group="test-group"):
        return ("fixture-platform", "test-bot", group, sender)

    async def history(event):
        manager = context.conversation_manager
        cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
        conv = await manager.get_conversation(event.unified_msg_origin, cid)
        return conv.history if conv else ""

    results = []
    try:
        logger.setLevel("WARNING")
        first = await run(message("小村长你怎么评价"))
        assert first.outputs == ["评价什么？"], (first.outputs, calls)
        assert key() in plugin.engine.lanes
        results.append("称呼开启窗口")
        follow = await run(message("评价电影星河"), answer="星河的画面很美。")
        assert follow.outputs == ["星河的画面很美。"]
        results.append("无关键词续聊")
        joined = await run(message("确实，星河画面不错", sender="bob"), answer="确实，画面是星河的亮点。")
        assert joined.outputs and key("bob") in plugin.engine.lanes
        plugin.engine.close(key("bob"))
        await run(message("说说轨道电梯"), answer="轨道电梯的材料强度还不够。")
        joined_question = await run(message("轨道电梯材料是什么", sender="bob"), answer="材料需要高比强度。")
        assert joined_question.outputs == ["材料需要高比强度。"] and key("bob") in plugin.engine.lanes
        before = len(calls)
        joined_expiry = plugin.engine.lanes[key("bob")].expires
        off_topic = await run(message("火锅在哪里吃", sender="bob"))
        assert not off_topic.outputs and len(calls) == before
        assert plugin.engine.lanes[key("bob")].expires == joined_expiry
        assert "火锅在哪里吃" not in await history(joined_question)
        assert (await run(message("这种材料能制造吗", sender="bob"), answer="可以制造小样。")).outputs
        results.append("加入者换题本地零调用且不续期，相关追问仍可接续")
        assert (await run(message("再详细说说", sender="bob"), answer="还需要考虑缆绳长度。")).outputs
        results.append("无固定句首的相关问题加入并建立新用户续聊窗口")
        before = len(calls)
        unrelated = await run(message("我也想去吃火锅", sender="carol"))
        assert not unrelated.outputs and len(calls) == before
        results.append("相关群友加入，无关插话零调用")
        parallel = await asyncio.gather(run(message("/讨论跑步", sender="bob"), answer="跑步要循序渐进。"),
                                        run(message("再谈星河结局"), answer="星河结局留下悬念。"))
        assert all(e.outputs for e in parallel)
        assert key() in plugin.engine.lanes and key("bob") in plugin.engine.lanes
        results.append("同群不同用户分别维护窗口")
        before = len(calls)
        ignored = await run(message("你晚上吃什么", other=True))
        assert not ignored.outputs and len(calls) == before
        results.append("明确他人指向零调用")
        expiry = plugin.engine.lanes[key()].expires
        silent = await run(message("我和别人说一下下周安排"), answer="<natural_followup_silent/>")
        assert not silent.outputs
        assert plugin.engine.lanes[key()].expires == expiry
        assert "natural_followup_silent" not in await history(first)
        results.append("沉默不发送不入历史不续期")
        before = len(calls)
        batch = [message("小村长"), message("你怎么评价"), message("三段合并")]
        await asyncio.gather(*(run(e, answer="合并回答") for e in batch))
        assert len(calls) == before + 1, len(calls) - before
        assert sum(len(e.outputs) for e in batch) == 1
        assert str(calls[-1]["request"]).count("三段合并") == 1
        results.append("三条短消息一次请求")
        old = message("小村长评价旧对象")
        task = asyncio.create_task(execute(old, answer="不应发送的旧答案", delay=.4))
        await asyncio.sleep(.15)
        correction = message("改为评价新对象")
        newer = asyncio.create_task(execute(correction, answer="新对象答案"))
        await asyncio.wait_for(asyncio.gather(task, newer), 15)
        await asyncio.sleep(0)
        assert not old.outputs, old.outputs
        assert correction.outputs == ["新对象答案"], correction.outputs
        assert "不应发送的旧答案" not in await history(old)
        results.append("生成中纠正抑制旧答案与历史")
        search = message("小村长搜索星河")
        search_task = asyncio.create_task(execute(search, tool=True, answer="已根据纠正使用同一份搜索结果"))
        await asyncio.wait_for(tool_started.wait(), 5)
        fix = message("纠正，关注上映年份")
        await asyncio.wait_for(asyncio.gather(search_task, asyncio.create_task(execute(fix))), 15)
        await asyncio.sleep(0)
        assert tool_calls == ["search"], tool_calls
        assert search.outputs == ["已根据纠正使用同一份搜索结果"], (search.outputs, fix.outputs)
        assert not fix.outputs
        search_requests = [c for c in calls if c["id"] == search.message_obj.message_id]
        assert len(search_requests) == 2 and "关注上映年份" in str(search_requests[-1]["request"])
        results.append("工具执行期间原生吸收纠正，工具仅执行一次")
        respond = next(s for s in scheduler.stages if type(s).__name__ == "RespondStage")
        respond.enable_seg, respond.interval_method, respond.interval = True, "random", [.3, .3]
        delayed = message("小村长测试发送前延迟")
        delayed_task = asyncio.create_task(execute(delayed, answer="等待发送的旧答案"))
        await asyncio.sleep(.15)
        latest = message("纠正，换个对象")
        await asyncio.wait_for(asyncio.gather(delayed_task, asyncio.create_task(execute(latest, answer="纠正后的答案"))), 15)
        respond.enable_seg = False
        assert not delayed.outputs and latest.outputs == ["纠正后的答案"]
        assert "等待发送的旧答案" not in await history(delayed)
        results.append("分段等待期间新纠正也能抑制旧答案")
        before = len(calls)
        duplicate = await run(message("小村长重复", mid=correction.message_obj.message_id))
        assert not duplicate.outputs and len(calls) == before
        results.append("重复事件零调用")
        expiry = plugin.engine.lanes[key()].expires
        failed = await run(message("小村长发送失败测试", fail=True), answer="未送达答案")
        assert not failed.outputs
        assert plugin.engine.lanes[key()].expires == expiry
        assert "未送达答案" not in await history(failed)
        results.append("发送失败不续期不入历史")
        # 图片生命周期另起独立情景，不沿用前面多项测试累计的每分钟配额。
        plugin.engine.lanes[key()].requests.clear()
        image = await run(message("", image=True), answer="看到图片")
        assert image.outputs == ["看到图片"]
        assert calls[-1]["request"].get("image_urls") or "image_url" in str(calls[-1]["request"])
        results.append("图片进入多模态请求")
        plugin.engine.lanes[key()].requests.clear()
        text_with_pending_image = message("小村长评价下一张图")
        image_task = asyncio.create_task(execute(text_with_pending_image, answer="图片到达前的旧答案", delay=.4))
        await asyncio.sleep(.15)
        pending_image = message("", image=True)
        pending_path = Path(f"data/temp/test-{pending_image.message_obj.message_id}.png")
        await asyncio.wait_for(asyncio.gather(image_task, asyncio.create_task(execute(pending_image, answer="追加图片回答"))), 15)
        assert not text_with_pending_image.outputs and pending_image.outputs == ["追加图片回答"]
        assert "image_url" in str(calls[-1]["request"]) or calls[-1]["request"].get("image_urls")
        assert not pending_image._temporary_local_files
        results.append("生成中补图不会被文字追加吞掉，媒体交给原生生命周期管理")
        plugin.engine.lanes[key()].requests.clear()
        fast_settings = plugin.engine.settings
        plugin.engine.settings = replace(fast_settings, debounce_seconds=1.2, max_wait_seconds=3)
        batch_text, batch_image = message("小村长看合并图片"), message("", image=True)
        before = len(calls)
        await asyncio.gather(run(batch_text, answer="合并图片回答"), run(batch_image, answer="合并图片回答"))
        assert len(calls) == before + 1 and batch_text.outputs + batch_image.outputs == ["合并图片回答"], (
            len(calls) - before, batch_text.outputs, batch_image.outputs,
            [(c["id"], c["request"].get("prompt")) for c in calls[before:]])
        assert calls[-1]["request"].get("image_urls") or "image_url" in str(calls[-1]["request"])
        assert Path(f"data/temp/test-{batch_image.message_obj.message_id}.png").exists()
        assert not batch_image._temporary_local_files
        plugin.engine.settings = fast_settings
        results.append("合并图片所有权转交主事件，框架保留的历史图片不会被后续事件误删")
        before = len(calls)
        foreign = await run(message("普通续聊", group="other-group"))
        assert not foreign.outputs and len(calls) == before
        results.append("未启用群零调用")
        for text in ["/help", "#help", "/  help"]:
            await run(message(text))
            assert len(calls) == before, text
        results.append("原生命令不调用模型")
        end = await run(message("先这样"))
        assert key() not in plugin.engine.lanes and not end.outputs
        before = len(calls)
        await run(message("继续刚才的话题"))
        assert len(calls) == before
        results.append("结束恢复原生触发")
        await run(message("小村长再次开始"))
        reset = await run(message("/reset"))
        assert reset.outputs and not plugin.engine.lanes, reset.outputs
        results.append("成功 reset 清理本群所有窗口")
        for alias in ["大肥鱼", "小鲸", "鲸鱼娘"]:
            assert (await run(message(alias + "你好"))).outputs
        assert (await run(message("普通提问", mention=True))).outputs
        results.append("四个称呼与原生 @ 无回归")
        plugin.engine.lanes[key()].expires = plugin.engine.clock() - 1
        before = len(calls)
        assert not (await run(message("到期后的普通聊天"))).outputs
        assert len(calls) == before
        results.append("到期恢复正常触发，零模型调用")
        print(json.dumps({"passed": results, "model_calls": len(calls)}, ensure_ascii=False))
    finally:
        await core.stop()
        await db_helper.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
