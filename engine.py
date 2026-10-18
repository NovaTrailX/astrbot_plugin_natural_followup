"""不依赖 AstrBot 的短时资格、候选筛选与批次协调。"""
import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field

from .settings import Settings

SILENCE = "<natural_followup_silent/>"
MAX_STATES = 128
MAX_ITEMS = 8
MAX_CHARS = 8000
ACKS = {"好", "好的", "嗯", "嗯嗯", "哦", "噢", "谢谢", "谢了", "哈哈", "哈哈哈", "ok", "收到"}
COMMON = {"这个", "那个", "什么", "怎么", "觉得", "就是", "还是", "可以", "知道", "一下", "没有", "是不是", "不是", "我们", "你们", "他们", "其实", "不过", "所以", "的话", "你说", "说的"}
COMMON |= {"我也", "我倒", "这部", "那部", "这点", "那么", "补充", "不对", "可是", "不错", "很好", "好看", "喜欢", "确实", "感觉", "真的", "比较", "有点", "特别", "可能", "认为"}
NON_TOPIC = re.compile("|".join(sorted(COMMON, key=len, reverse=True)))


def plain_end(text):
    return text.strip().rstrip("，,。.!！?？~～ ").lower()


def terms(text):
    result = set(re.findall(r"[A-Za-z0-9_]{3,}", text.lower()))
    for word in re.findall(r"[\u4e00-\u9fff]+", NON_TOPIC.sub(" ", text)):
        result.update(word[i:i + 2] for i in range(len(word) - 1))
    return result - COMMON


def asks_question(text):
    return bool(re.search(r"[?？]|(?:吗|么|呢)[呀啊。！! ]*$|说清楚|请告诉|请提供|哪个|哪种|是否", text[-400:]))


@dataclass
class Item:
    message_id: str
    text: str
    event: object = None
    images: bool = False


@dataclass
class Batch:
    key: tuple
    reason: str
    items: list[Item]
    created: float
    updated: float
    revision: int
    covered: int
    done: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    sealed: bool = False
    cancelled: bool = False
    running: bool = False
    raw_reply: str = ""
    suppressed: bool = False
    silent: bool = False
    run_context: object = None
    conversation: str | None = None
    parent: object = None
    original_send: object = None
    delivered: bool = False
    latest_text: str = ""
    final_chain: object = None

    @property
    def explicit(self):
        return self.reason in {"explicit", "named"}

    @property
    def text(self):
        return "\n".join(i.text for i in self.items if i.text)


@dataclass
class Lane:
    conversation: str | None
    expires: float = 0
    last_sent: float = 0
    user_text: str = ""
    bot_text: str = ""
    revision: int = 0
    batches: list[Batch] = field(default_factory=list)
    recent_ids: deque = field(default_factory=lambda: deque(maxlen=64))
    requests: deque = field(default_factory=deque)


class Engine:
    def __init__(self, settings: Settings, clock=time.monotonic):
        self.settings = settings
        self.clock = clock
        self.lanes: dict[tuple, Lane] = {}
        self.group_requests: dict[tuple, deque] = {}
        self.join_requests: dict[tuple, deque] = {}
        aliases = "|".join(re.escape(n) for n in settings.aliases) or r"(?!)"
        self.named = re.compile(
            rf"^(?:(?:喂|嘿|诶|欸|哎|你好|您好|请问)[，,、:：\s]*)?(?:{aliases})"
            rf"|[，,、:：\s]+(?:{aliases})[，,。.!！?？~～\s]*$"
        )

    def clear_scope(self, scope):
        for key in list(self.lanes):
            if key[:-1] == scope:
                self.close(key)
        self.group_requests.pop(scope, None)
        self.join_requests.pop(scope, None)

    def close(self, key):
        lane = self.lanes.pop(key, None)
        if lane:
            for batch in lane.batches:
                batch.cancelled = True
                batch.changed.set()
                batch.done.set()

    def prune(self):
        now = self.clock()
        for key, lane in list(self.lanes.items()):
            # 同一人的悬挂管线和等待批次一起失效，不让媒体批次越过仍在运行的旧请求。
            if any(now - batch.created >= 600 for batch in lane.batches):
                self.close(key)
                continue
            if not lane.batches and now >= lane.expires and (not lane.requests or now - lane.requests[-1] >= 60):
                self.close(key)
        for mapping in (self.group_requests, self.join_requests):
            for scope, queue in list(mapping.items()):
                if not queue or now - queue[-1] >= 60:
                    del mapping[scope]

    def candidate(self, key, text, *, explicit=False, other_target=False, image=False, conversation=None):
        now = self.clock()
        if any(k[:-1] == key[:-1] and v.conversation is not None and v.conversation != conversation
               for k, v in self.lanes.items()):
            self.clear_scope(key[:-1])
        lane = self.lanes.get(key)
        if lane and lane.conversation != conversation:
            if lane.conversation is None and lane.batches:
                lane.conversation = conversation
                for batch in lane.batches:
                    batch.conversation = conversation
            else:
                self.clear_scope(key[:-1])
                lane = None
        text = text.strip()
        named = bool(self.named.search(text))
        closing = text
        for alias in self.settings.aliases:
            if closing.startswith(alias):
                closing = closing[len(alias):].lstrip("，,：: ")
                break
        if lane and plain_end(closing) in {plain_end(x) for x in self.settings.end_phrases}:
            self.close(key)
            return "end"
        if explicit or named:
            return "explicit" if explicit else "named"
        if other_target or (not text and not image) or (image and not text and not self.settings.image_followup):
            return None
        if lane and (now < lane.expires or lane.batches):
            if not image and plain_end(text) in ACKS and not asks_question(lane.bot_text):
                return None
            return "followup"
        if image or not self.settings.join_seconds:
            return None
        if not re.search(r"^(?:我也|我倒|其实|不过|可是|不对|补充|这部|这点|这个|那部|那么|所以)", text):
            return None
        matches = []
        text_terms = terms(text)
        for other_key, other in self.lanes.items():
            if other_key[:-1] != key[:-1] or not other.last_sent or now - other.last_sent > self.settings.join_seconds:
                continue
            if text_terms & terms(other.user_text + other.bot_text):
                matches.append(other)
        return "join" if len(matches) == 1 else None

    def submit(self, key, reason, item, conversation=None):
        self.prune()
        lane = self.lanes.get(key)
        if lane is None:
            if len(self.lanes) >= MAX_STATES:
                return None, False
            lane = self.lanes[key] = Lane(conversation=conversation)
        if item.message_id in lane.recent_ids:
            return None, False
        batch = next((b for b in reversed(lane.batches) if not b.sealed and not b.cancelled), None)
        now = self.clock()
        if batch and (len(batch.items) >= MAX_ITEMS or len(batch.text) + len(item.text) > MAX_CHARS):
            batch.sealed = True
            batch.changed.set()
            batch = None
        if batch is None and len(lane.batches) >= 4:
            return None, False
        lane.recent_ids.append(item.message_id)
        lane.revision += 1
        if batch:
            batch.items.append(item)
            batch.updated = now
            batch.revision = batch.covered = lane.revision
            if reason in {"explicit", "named"}:
                batch.reason = reason
            batch.changed.set()
            return batch, False
        batch = Batch(key, reason, [item], now, now, lane.revision, lane.revision, conversation=conversation)
        if not self.allow(batch):
            return None, False
        batch.parent = next((b for b in reversed(lane.batches) if b.running and not b.done.is_set()), None)
        lane.batches.append(batch)
        return batch, True

    async def collect(self, batch):
        while not batch.sealed and not batch.cancelled:
            now = self.clock()
            delay = min(batch.updated + self.settings.debounce_seconds, batch.created + self.settings.max_wait_seconds) - now
            if delay <= 0 or len(batch.items) >= MAX_ITEMS or len(batch.text) >= MAX_CHARS:
                break
            batch.changed.clear()
            try:
                await asyncio.wait_for(batch.changed.wait(), delay)
            except TimeoutError:
                break
        batch.sealed = True
        return not batch.cancelled

    def allow(self, batch):
        if batch.explicit:
            return True
        now = self.clock()
        lane = self.lanes.get(batch.key)
        if lane is None:
            return False
        group = self.group_requests.setdefault(batch.key[:-1], deque())
        checks = [(lane.requests, 60, self.settings.user_batches_per_minute),
                  (group, 60, self.settings.group_batches_per_minute)]
        if batch.reason == "join":
            joins = self.join_requests.setdefault(batch.key[:-1], deque())
            checks.append((joins, max(1, self.settings.join_seconds), self.settings.join_batches_per_window))
        for queue, window, limit in checks:
            while queue and now - queue[0] >= window:
                queue.popleft()
            if len(queue) >= limit:
                return False
        for queue, _, _ in checks:
            queue.append(now)
        return True

    def stale(self, batch):
        lane = self.lanes.get(batch.key)
        return batch.cancelled or lane is None or any(
            b.revision > batch.covered and not b.cancelled for b in lane.batches
        )

    def absorb(self, child):
        parent = child.parent
        if parent and not parent.done.is_set():
            parent.covered = max(parent.covered, child.covered)
            parent.latest_text = child.text[-800:]

    def sent(self, batch, text):
        lane = self.lanes.get(batch.key)
        if lane is None or batch.cancelled or batch.suppressed or batch.silent:
            return
        now = self.clock()
        lane.last_sent = now
        lane.expires = now + self.settings.idle_seconds
        lane.user_text = (batch.latest_text or batch.text)[-800:]
        lane.bot_text = text[-800:]

    def finish(self, batch):
        batch.done.set()
        batch.changed.set()
        lane = self.lanes.get(batch.key)
        if lane and batch in lane.batches:
            lane.batches.remove(batch)

    def shutdown(self):
        for key in list(self.lanes):
            self.close(key)
        self.group_requests.clear()
        self.join_requests.clear()
