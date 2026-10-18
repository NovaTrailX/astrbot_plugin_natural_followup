"""验证交互边界；不依赖 QQ、网络和模型。"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_natural_followup.engine import Engine, Item, MAX_STATES
from astrbot_plugin_natural_followup.settings import Settings

A = ("platform-a", "bot-a", "group-a", "user-a")
B = (*A[:-1], "user-b")


class Rules(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.engine = Engine(Settings(aliases=["小村长", "大肥鱼", "小鲸", "鲸鱼娘"]), lambda: self.now)

    def reply(self, key=A, user="评价电影星河", bot="你想听剧情还是画面？", mid="one"):
        batch, _ = self.engine.submit(key, "explicit", Item(mid, user), "c1")
        self.engine.sent(batch, bot)
        self.engine.finish(batch)
        return batch

    def candidate(self, text, key=A, **kwargs):
        return self.engine.candidate(key, text, conversation="c1", **kwargs)

    def test_aliases_and_native_triggers(self):
        for text in ["小村长你怎么评价", "大肥鱼来看看", "小鲸", "鲸鱼娘你好", "你怎么看，小村长"]:
            self.assertEqual(self.candidate(text), "named")
        self.assertIsNone(self.candidate("我昨天看到了小村长的消息"))
        self.assertEqual(self.candidate("一般问题", explicit=True), "explicit")

    def test_unrestricted_followup_and_expiry(self):
        self.reply()
        self.assertEqual(self.candidate("评价星河的最后一幕"), "followup")
        self.now += 89
        self.assertEqual(self.candidate("再具体一点"), "followup")
        self.now += 1
        self.assertIsNone(self.candidate("再具体一点"))

    def test_only_success_renews(self):
        self.reply()
        self.now += 60
        for flag in ("silent", "cancelled", "suppressed"):
            batch, _ = self.engine.submit(A, "followup", Item(flag, "补充"), "c1")
            setattr(batch, flag, True)
            self.engine.sent(batch, "回答")
            self.engine.finish(batch)
        self.assertEqual(self.engine.lanes[A].expires, 1090)
        batch, _ = self.engine.submit(A, "followup", Item("success", "再具体一点"), "c1")
        self.engine.sent(batch, "具体回答")
        self.assertEqual(self.engine.lanes[A].expires, 1150)

    def test_acknowledgement_depends_on_question(self):
        self.reply(bot="要我举个例子吗？")
        self.assertEqual(self.candidate("好"), "followup")
        self.engine.lanes[A].bot_text = "这就是我的看法。"
        self.assertIsNone(self.candidate("好"))

    def test_target_and_image(self):
        self.reply()
        self.assertIsNone(self.candidate("你怎么看", other_target=True))
        self.assertEqual(self.candidate("", image=True), "followup")
        self.assertIsNone(self.candidate(""))
        self.assertEqual(self.candidate("小村长你怎么看", other_target=True), "named")

    def test_join_requires_unique_anchor(self):
        self.reply()
        self.assertEqual(self.candidate("我也觉得星河画面不错", B), "join")
        self.assertIsNone(self.candidate("我也想去吃火锅", B))
        self.assertIsNone(self.candidate("今天星河很好看", B))
        self.reply((*A[:-1], "user-c"), mid="c")
        self.assertIsNone(self.candidate("我也觉得星河画面不错", B))

    def test_join_window_closes(self):
        self.reply()
        self.now += 21
        self.assertIsNone(self.candidate("我也觉得星河画面不错", B))

    def test_join_cues_are_not_topic_overlap(self):
        self.reply(user="我也觉得星河不错", bot="确实很好看。")
        self.assertIsNone(self.candidate("我也觉得火锅不错", B))

    def test_agreement_needs_actual_topic_and_unique_anchor(self):
        self.reply(user="星河怎么样", bot="特效很用心。")
        for cue in ["确实", "同感", "对啊", "是啊"]:
            with self.subTest(cue=cue):
                self.assertEqual(self.candidate(cue + "，特效不错", B), "join")
                self.assertIsNone(self.candidate(cue, B))
                self.assertIsNone(self.candidate(cue + "，火锅不错", B))
        self.reply((*A[:-1], "user-c"), mid="c", bot="特效很用心。")
        self.assertIsNone(self.candidate("确实，特效不错", B))

    def test_isolation(self):
        self.reply()
        for key in [("platform-b", *A[1:]), (A[0], "bot-b", *A[2:]), (*A[:2], "group-b", A[3]), B]:
            self.assertIsNone(self.candidate("评价星河", key))

    def test_join_topic_question_without_fixed_prefix(self):
        self.reply(user="评价星河", bot="轨道电梯那段画面很好。")
        self.now += 13
        self.assertEqual(self.candidate("轨道电梯材料是什么", B), "join")
        self.assertEqual(self.candidate("轨道电梯吗？我觉得还行", B), "join")
        self.assertIsNone(self.candidate("晚上吃火锅吗", B))
        self.assertIsNone(self.candidate("轨道电梯材料是什么", B, other_target=True))
        self.now += 8
        self.assertIsNone(self.candidate("轨道电梯材料是什么", B))

    def test_topic_join_does_not_cross_ambiguous_anchors(self):
        self.reply(user="聊轨道电梯", bot="轨道电梯的材料强度还不够。")
        self.reply((*A[:-1], "user-c"), mid="c", user="轨道电梯还有什么问题")
        self.assertIsNone(self.candidate("轨道电梯怎么供电", B))

    def test_new_joiner_gets_own_window_only_after_reply(self):
        self.reply(user="聊轨道电梯", bot="轨道电梯的材料强度还不够。")
        reason = self.candidate("轨道电梯怎么供电", B)
        self.assertEqual(reason, "join")
        batch, _ = self.engine.submit(B, reason, Item("joined", "轨道电梯怎么供电"), "c1")
        self.engine.sent(batch, "可以讨论供电方式。")
        self.engine.finish(batch)
        self.now += 21
        self.assertEqual(self.candidate("再详细说说", B), "followup")

    def test_joined_window_filters_topic_switch_without_renewal(self):
        self.reply(user="聊轨道电梯", bot="轨道电梯材料强度很重要。")
        batch, _ = self.engine.submit(B, "join", Item("joined", "轨道电梯用什么材料"), "c1")
        self.engine.sent(batch, "需要高强度材料。")
        self.engine.finish(batch)
        lane = self.engine.lanes[B]
        before = (lane.expires, list(lane.requests), lane.revision)
        for text in ["火锅在哪里吃", "今晚吃火锅", "那火锅在哪里吃", "确实，火锅好吃"]:
            self.assertIsNone(self.candidate(text, B), text)
        self.assertEqual((lane.expires, list(lane.requests), lane.revision), before)
        for text in ["这种材料现在能造出来吗", "轨道电梯怎么供电", "再详细说说", "有多贵", "那它有多贵？"]:
            self.assertEqual(self.candidate(text, B), "followup", text)
        self.assertEqual(self.candidate("", B, image=True), "followup")

    def test_joined_window_explicit_trigger_can_change_topic(self):
        self.reply()
        joined, _ = self.engine.submit(B, "join", Item("joined", "星河画面不错"), "c1")
        self.engine.sent(joined, "特效用心。")
        self.engine.finish(joined)
        self.assertEqual(self.candidate("小村长推荐火锅", B), "named")
        explicit, _ = self.engine.submit(B, "named", Item("new-topic", "小村长推荐火锅"), "c1")
        self.assertEqual(self.candidate("想吃辣一点", B), "followup")
        self.engine.sent(explicit, "喜欢哪种口味？")
        self.engine.finish(explicit)
        self.assertFalse(self.engine.lanes[B].join_topic)
        self.assertEqual(self.candidate("辣一点的", B), "followup")

    def test_joined_pending_reply_ignores_unrelated_correction(self):
        self.reply(user="轨道电梯", bot="需要高强度材料。")
        joined, _ = self.engine.submit(B, "join", Item("joined", "轨道电梯用什么材料"), "c1")
        self.assertIsNone(self.candidate("火锅在哪里吃", B))
        self.assertFalse(self.engine.stale(joined))

    def test_topic_does_not_join_across_message_boundary(self):
        self.reply(user="轨道电", bot="梯车改造。")
        self.assertIsNone(self.candidate("电梯车哪个好", B))

    def test_end_closes_only_sender(self):
        self.reply()
        self.reply(B, mid="b")
        self.assertEqual(self.candidate("小村长，不用回了。"), "end")
        self.assertNotIn(A, self.engine.lanes)
        self.assertIn(B, self.engine.lanes)
        self.assertIsNone(self.candidate("他说不用回了"))

    def test_conversation_change_clears_group(self):
        self.reply()
        self.reply(B, mid="b")
        self.assertIsNone(self.engine.candidate(A, "继续", conversation="c2"))
        self.assertFalse(self.engine.lanes)

    def test_initial_conversation_binding(self):
        batch, _ = self.engine.submit(A, "named", Item("first", "小村长你好"))
        self.assertEqual(self.engine.candidate(A, "补充", conversation="created"), "followup")
        self.assertFalse(batch.cancelled)
        self.assertEqual(batch.conversation, "created")

    def test_duplicate_and_batch_limit(self):
        first, leader = self.engine.submit(A, "named", Item("1", "小村长你好"))
        self.assertTrue(leader)
        duplicate, _ = self.engine.submit(A, "named", Item("1", "小村长你好"))
        self.assertIsNone(duplicate)
        for n in range(2, 9):
            batch, leader = self.engine.submit(A, "followup", Item(str(n), "补充"))
            self.assertIs(first, batch)
            self.assertFalse(leader)
        ninth, leader = self.engine.submit(A, "followup", Item("9", "最后补充"))
        self.assertTrue(leader)
        self.assertIsNot(first, ninth)
        self.assertTrue(first.sealed)

    def test_correction_and_absorption(self):
        first, _ = self.engine.submit(A, "named", Item("1", "评价甲"))
        first.sealed = first.running = True
        second, _ = self.engine.submit(A, "followup", Item("2", "改为评价乙"))
        self.assertTrue(self.engine.stale(first))
        self.engine.absorb(second)
        self.engine.finish(second)
        self.assertFalse(self.engine.stale(first))
        self.assertEqual(first.latest_text, "改为评价乙")

    def test_limits_do_not_suppress_answer_or_limit_explicit(self):
        self.engine = Engine(Settings(user_batches_per_minute=1), lambda: self.now)
        first, _ = self.engine.submit(A, "followup", Item("1", "甲"))
        first.sealed = first.running = True
        second, _ = self.engine.submit(A, "followup", Item("2", "乙"))
        self.assertIsNone(second)
        self.assertFalse(self.engine.stale(first))
        explicit, _ = self.engine.submit(A, "explicit", Item("3", "丙"))
        self.assertIsNotNone(explicit)

    def test_capacity_and_cleanup(self):
        for n in range(MAX_STATES):
            self.reply((*A[:-1], str(n)), mid=str(n))
        extra, _ = self.engine.submit(B, "explicit", Item("extra", "你好"))
        self.assertIsNone(extra)
        self.now += 91
        self.engine.prune()
        self.assertFalse(self.engine.lanes)

    def test_shutdown_releases_waiters(self):
        batch, _ = self.engine.submit(A, "explicit", Item("1", "你好"))
        self.engine.shutdown()
        self.assertTrue(batch.cancelled)
        self.assertTrue(batch.done.is_set())

    def test_char_limit_splits_without_truncation(self):
        first, _ = self.engine.submit(A, "explicit", Item("1", "甲" * 7900))
        second, leader = self.engine.submit(A, "followup", Item("2", "乙" * 200))
        self.assertTrue(leader)
        self.assertTrue(first.sealed)
        self.assertEqual(len(first.text) + len(second.text), 8100)

    def test_pending_batches_are_bounded(self):
        for n in range(4):
            batch, _ = self.engine.submit(A, "explicit", Item(str(n), "长任务"))
            batch.sealed = True
        rejected, _ = self.engine.submit(A, "followup", Item("overflow", "补充"))
        self.assertIsNone(rejected)
        self.assertEqual(len(self.engine.lanes[A].batches), 4)

    def test_hung_lane_cleanup_releases_all_batches(self):
        first, _ = self.engine.submit(A, "explicit", Item("1", "长任务"))
        first.sealed = first.running = True
        self.now += 2
        child, _ = self.engine.submit(A, "followup", Item("2", "", images=True))
        self.now += 598
        self.engine.prune()
        self.assertTrue(first.cancelled and child.cancelled)
        self.assertTrue(first.done.is_set() and child.done.is_set())
        self.assertNotIn(A, self.engine.lanes)

    def test_group_quota_shared_across_users(self):
        self.engine = Engine(Settings(group_batches_per_minute=1), lambda: self.now)
        first, _ = self.engine.submit(A, "followup", Item("1", "甲"))
        self.assertIsNotNone(first)
        second, _ = self.engine.submit(B, "followup", Item("2", "乙"))
        self.assertIsNone(second)
        self.now += 60
        second, _ = self.engine.submit(B, "followup", Item("3", "乙"))
        self.assertIsNotNone(second)

    def test_join_quota(self):
        for n in range(2):
            batch, _ = self.engine.submit((*A[:-1], str(n)), "join", Item(str(n), "补充"))
            self.assertIsNotNone(batch)
        batch, _ = self.engine.submit(B, "join", Item("third", "补充"))
        self.assertIsNone(batch)

    def test_new_sender_cannot_join_previous_conversation(self):
        self.reply()
        self.assertIsNone(self.engine.candidate(B, "我也觉得星河画面不错", conversation="c2"))
        self.assertFalse(self.engine.lanes)


class Collection(unittest.IsolatedAsyncioTestCase):
    async def test_three_messages_one_batch(self):
        engine = Engine(Settings(debounce_seconds=.03, max_wait_seconds=.1))
        first, _ = engine.submit(A, "named", Item("1", "小村长"))
        waiter = asyncio.create_task(engine.collect(first))
        await asyncio.sleep(.01)
        engine.submit(A, "followup", Item("2", "你怎么评价"))
        await asyncio.sleep(.01)
        engine.submit(A, "followup", Item("3", "电影星河"))
        self.assertTrue(await waiter)
        self.assertEqual(first.text, "小村长\n你怎么评价\n电影星河")
        self.assertEqual(len(engine.lanes[A].batches), 1)

    async def test_unload_interrupts_wait(self):
        engine = Engine(Settings())
        batch, _ = engine.submit(A, "named", Item("1", "小村长"))
        waiter = asyncio.create_task(engine.collect(batch))
        await asyncio.sleep(0)
        engine.shutdown()
        self.assertFalse(await asyncio.wait_for(waiter, .1))


class Configuration(unittest.TestCase):
    def test_defaults_and_boundaries(self):
        self.assertEqual(Settings.load({}).enabled_groups, [])
        for invalid in [{"idle_seconds": -1}, {"join_seconds": 91},
                        {"debounce_seconds": 2, "max_wait_seconds": 1},
                        {"max_wait_seconds": float("nan")}, {"enabled": "yes"},
                        {"aliases": [""]}, {"user_batches_per_minute": True}]:
            with self.subTest(config=invalid), self.assertRaises(ValueError):
                Settings.load(invalid)


if __name__ == "__main__":
    unittest.main()
