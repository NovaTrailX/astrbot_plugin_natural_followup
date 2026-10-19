"""通过标准事件钩子接入 AstrBot；不重新分发消息或接管群历史。"""
import asyncio
from contextlib import suppress

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, AtAll, Image, Plain, Reply
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter

from .engine import Batch, Engine, Item, SILENCE
from .settings import Settings

KEY = "_natural_followup_batch"


@register("astrbot_plugin_natural_followup", "NovaTrailX", "有界、可沉默的 QQ 群自然续聊", "0.1.1")
class NaturalFollowup(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.context = context
        self.settings = Settings.load(config)
        self.engine = Engine(self.settings)
        self.cleaner = asyncio.create_task(self._cleanup())

    async def _cleanup(self):
        while True:
            await asyncio.sleep(30)
            self.engine.prune()

    async def terminate(self):
        self.cleaner.cancel()
        self.engine.shutdown()
        with suppress(asyncio.CancelledError):
            await self.cleaner

    def scope(self, event):
        if not self.settings.enabled or event.get_platform_name() != "aiocqhttp" or not event.get_group_id():
            return None
        source = f"{event.get_platform_id()}:GroupMessage:{event.get_group_id()}"
        if source not in self.settings.enabled_groups:
            return None
        return event.get_platform_id(), event.get_self_id(), event.get_group_id()

    def log(self, reason, **counts):
        if self.settings.debug_stats:
            logger.info("[NaturalFollowup] %s %s", reason, counts)

    def _complete(self, event, batch):
        if batch.original_send is not None:
            event.send = batch.original_send
        captured = event.get_extra("_follow_up_captured")
        if captured and batch.parent:
            parent_id = str(batch.parent.items[0].event.message_obj.message_id)
            if captured.get("target_run_id") == parent_id:
                self.engine.absorb(batch)
        self.engine.finish(batch)
        self.log("pipeline_done", items=len(batch.items), suppressed=batch.suppressed, silent=batch.silent,
                 elapsed_ms=round((self.engine.clock() - batch.created) * 1000))

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=-100000)
    async def on_message(self, event: AstrMessageEvent):
        scope = self.scope(event)
        if scope is None or event.get_sender_id() == event.get_self_id():
            return
        if event.is_stopped() or event._has_send_oper or event.call_llm:
            return
        if event.get_extra("_group_participation_reason") == "open_question":
            return
        handlers = event.get_extra("activated_handlers", [])
        if any(isinstance(f, (CommandFilter, CommandGroupFilter)) for h in handlers for f in h.event_filters):
            return
        config = self.context.get_config(umo=event.unified_msg_origin)
        if config.get("agent_runner", {}).get("runner_type", "local") != "local":
            return
        messages = event.get_messages()
        text = event.get_message_str().strip()
        images = any(isinstance(c, Image) for c in messages)
        other = any(
            isinstance(c, AtAll)
            or isinstance(c, At) and str(c.qq) != str(event.get_self_id())
            or isinstance(c, Reply) and str(c.sender_id) != str(event.get_self_id())
            for c in messages
        )
        conversation = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        key = (*scope, event.get_sender_id())
        lane = self.engine.lanes.get(key)
        if lane and str(event.message_obj.message_id) in lane.recent_ids:
            event.should_call_llm(True)
            self.log("duplicate")
            return
        reason = self.engine.candidate(key, text, explicit=event.is_at_or_wake_command,
                                       other_target=other, image=images, conversation=conversation)
        if reason == "end":
            event.should_call_llm(True)
            self.log("closed")
            return
        if reason is None:
            return
        # 额外称呼在容量不足时仍保留原生直接回答，只有续聊状态不再扩张。
        if reason == "named":
            event.is_at_or_wake_command = True
        item = Item(str(event.message_obj.message_id), text, event, images)
        batch, leader = self.engine.submit(key, reason, item, conversation)
        if batch is None:
            self.log("duplicate_or_capacity")
            return
        event.set_extra(KEY, batch)
        if not leader:
            # 等首事件完成后再结束后续事件，确保临时图片仍然可读。
            event.should_call_llm(True)
            await batch.done.wait()
            return
        task = asyncio.current_task()
        task.add_done_callback(lambda _: self._complete(event, batch))
        if not await self.engine.collect(batch):
            batch.cancelled = True
            event.should_call_llm(True)
            self.log("cancelled_or_limited")
            return
        lane = self.engine.lanes.get(key)
        if lane:
            batch.parent = next((b for b in reversed(lane.batches)
                                 if b is not batch and b.running and not b.done.is_set()), None)
        # 原生 follow_up 只接收文字；媒体必须留到下一轮多模态请求。
        if batch.parent and any(i.images for i in batch.items):
            await batch.parent.done.wait()
            if batch.cancelled:
                event.should_call_llm(True)
                return
        components = []
        for entry in batch.items:
            components.extend(entry.event.get_messages())
            if entry.event is not event:
                # 图片可能被原生历史持久化逻辑解除临时跟踪；必须把所有权交给
                # 主事件，否则后续事件退出时仍会删除框架决定保留的媒体。
                for path in list(entry.event._temporary_local_files):
                    event.track_temporary_local_file(path)
                    entry.event.untrack_temporary_local_file(path)
        event.message_obj.message = components
        event.message_str = batch.text
        event.message_obj.message_str = batch.text
        event.is_at_or_wake_command = True
        event.set_extra("_natural_followup_managed", True)
        event.set_extra("enable_streaming", False)
        event.set_extra("_natural_followup_message_ids", [i.message_id for i in batch.items])
        event.set_extra("_group_participation_reason", "named" if batch.explicit else "followup")
        self.log("accepted", trigger=batch.reason, batches=1, items=len(batch.items), explicit=batch.explicit)

    @filter.on_llm_request(priority=-100000)
    async def on_request(self, event, req):
        batch = event.get_extra(KEY)
        if batch is None:
            return
        if batch.cancelled:
            event.stop_event()
            return
        batch.running = True
        lane = self.engine.lanes[batch.key]
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        if not cfg.get("provider_settings", {}).get("identifier"):
            req.extra_user_content_parts.append(TextPart(
                text=f"User ID: {event.get_sender_id()}, Nickname: {event.get_sender_name()}"
            ))
        if req.conversation:
            current = req.conversation.cid
            # 首次显式触发可能由框架刚创建 conversation。
            if batch.conversation is not None and current != batch.conversation:
                self.engine.clear_scope(batch.key[:-1])
                event.stop_event()
                return
            lane.conversation = batch.conversation = current
        parts = []
        if not batch.explicit:
            parts.append(
                "这是有时限的群聊续聊候选，不是必须回答的唤醒。先结合说话人、你刚才的问题和当前内容判断对方是否在接续你。"
                "短答、补充对象、纠正和确认都可能是有效续聊；明显在与别人说话、话题无关或只是在收尾时不要插话，"
                f"不要调用工具，只输出 {SILENCE}。需要回答时直接按原有人格回复，不解释判断过程。"
            )
            if lane.join_topic:
                parts.append(
                    "当前发送者通过相关插话加入，免唤醒资格仅用于延续加入时的话题。"
                    "群内别人的闲聊和这个发送者后来无关的发言，都不构成换题邀请；"
                    f"如果当前文字或图片已经换题，只输出 {SILENCE}，不调用工具。"
                )
        if lane.user_text and lane.bot_text:
            # 每用户短锚点只补本次定位；已有历史则不再次复制正文。
            parts.append("本次优先承接当前发送者与你的往返，其他群友的话不要当成该用户说的。")
        if parts:
            # 每轮临时规则放入系统区，避免被当作用户原话反复写入聊天历史。
            req.system_prompt = (req.system_prompt or "") + "\n\n" + "\n".join(parts)

    def _sync_consumed(self, batch):
        lane = self.engine.lanes.get(batch.key)
        if lane:
            for child in list(lane.batches):
                captured = child.items[0].event.get_extra("_follow_up_captured")
                if (child.parent is batch and captured
                        and captured.get("target_run_id") == batch.items[0].message_id):
                    self.engine.absorb(child)
                    child.parent = None

    @filter.on_llm_response(priority=100000)
    async def on_response(self, event, resp):
        batch = event.get_extra(KEY)
        if batch is None or resp is None or resp.role != "assistant":
            return
        self._sync_consumed(batch)
        batch.raw_reply = resp.completion_text or ""
        batch.silent = not batch.explicit and batch.raw_reply.strip() == SILENCE
        batch.suppressed = self.engine.stale(batch)
        if batch.silent or batch.suppressed:
            # 先让其他响应钩子看不到内部标记或过时答案。
            resp.completion_text = ""
            resp.result_chain = None
        if batch.silent or batch.cancelled:
            event.stop_event()

    @filter.on_agent_done(priority=100000)
    async def on_done(self, event, run_context, response):
        batch = event.get_extra(KEY)
        if batch is None or response is None:
            return
        batch.run_context = run_context
        if response.result_chain:
            batch.final_chain = list(response.result_chain.chain)
        elif batch.raw_reply:
            batch.final_chain = [Plain(batch.raw_reply)]
        if batch.suppressed and not batch.cancelled:
            # 保留原生用户/完整工具链保存流程，但不保存未发送的最终答案。
            response.completion_text = batch.raw_reply
            self._hide_last_answer(batch)

    @staticmethod
    def _hide_last_answer(batch):
        if batch.run_context and batch.run_context.messages:
            last = batch.run_context.messages[-1]
            if last.role == "assistant" and not getattr(last, "tool_calls", None):
                last._no_save = True

    @filter.on_decorating_result(priority=100000)
    async def final_answer_only(self, event):
        batch = event.get_extra(KEY)
        result = event.get_result()
        if batch is not None and batch.final_chain is not None and result:
            # 原生缓冲可能拼接工具前说明与最终答案；先取最终链，再交给正常装饰插件。
            result.chain = list(batch.final_chain)

    @filter.on_decorating_result(priority=-100000)
    async def on_result(self, event):
        batch = event.get_extra(KEY)
        if batch is None:
            return
        if batch.run_context is None:
            # 工具执行前的中间文字尚未包含可能到达的纠正，等待最终回答。
            event.clear_result()
            return
        # 再检查一次，覆盖模型完成至发送之间到达的补充。
        batch.suppressed = batch.suppressed or self.engine.stale(batch)
        if batch.suppressed or batch.silent:
            self._hide_last_answer(batch)
            event.clear_result()
            return
        if batch.original_send is None:
            # 装饰钩子之后框架可能等待分段延迟；仅包装这个事件的发送，
            # 在实际提交 QQ 前再检查，任务结束立即还原，不修改适配器或核心。
            batch.original_send = event.send

            async def checked_send(chain):
                if self.engine.stale(batch):
                    batch.suppressed = True
                    self._hide_last_answer(batch)
                    return
                await batch.original_send(chain)
                batch.delivered = True
                event.set_extra("_natural_followup_delivered", True)
                actual = chain.get_plain_text() if chain else ""
                if actual:
                    prior = event.get_extra("_natural_followup_sent_text", "")
                    event.set_extra("_natural_followup_sent_text", "\n".join(x for x in [prior, actual] if x))

            event.send = checked_send

    @filter.after_message_sent(priority=-100000)
    async def after_sent(self, event):
        scope = self.scope(event)
        if scope is None:
            return
        if event.get_extra("_clean_group_context_session") or event.get_extra("_clean_ltm_session"):
            self.engine.clear_scope(scope)
            return
        batch = event.get_extra(KEY)
        if batch is None or not batch.raw_reply:
            return
        if not batch.delivered:
            self._hide_last_answer(batch)
            return
        result = event.get_result()
        text = result.get_plain_text() if result else batch.raw_reply
        self.engine.sent(batch, text)
        self.log("sent", items=len(batch.items))
