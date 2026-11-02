"""好友申请处理纯逻辑：资格判定、名单、每日限额、主人裁决（不依赖 AstrBot，可单测）。

只同意"看起来是真人的"申请：QQ 等级够高、开过会员、或是老号（QQ 号位数少）。
等级低又没会员的新号先挂着，把对方信息发给主人裁决——主人回复「同意 QQ号」
或「拒绝 QQ号」后再真正处理申请。这是防批量小号加好友的主要手段。
"""

from __future__ import annotations

import re
import time

DECISION_ACCEPT = "accept"
DECISION_REJECT = "reject"
DECISION_PENDING = "pending"

_NUM_SPLIT = re.compile(r"[,，\s]+")


def parse_qq_list(raw: str) -> set[str]:
    """解析逗号/空格分隔的 QQ 号列表。"""
    return {x.strip() for x in _NUM_SPLIT.split(str(raw or "")) if x.strip().isdigit()}


def extract_level(info: dict) -> int:
    """从 get_stranger_info 结果里取 QQ 等级。兼容多种字段名。"""
    if not isinstance(info, dict):
        return 0
    for key in ("qqLevel", "qq_level", "level", "qq_level_num"):
        value = info.get(key)
        try:
            level = int(value)
        except (TypeError, ValueError):
            continue
        if level > 0:
            return level
    return 0


def is_vip(info: dict) -> bool:
    """是否会员（VIP/SVIP）。NapCat 返回 vip_level 或 vipFlag。"""
    if not isinstance(info, dict):
        return False
    for key in ("vip_level", "vipLevel", "vip_flag", "vipFlag"):
        value = info.get(key)
        if isinstance(value, bool):
            if value:
                return True
            continue
        try:
            if int(value) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def evaluate(
    qq: str,
    *,
    info: dict,
    min_level: int = 16,
    accept_vip: bool = True,
    max_qq_len: int = 9,
    whitelist: set[str] | None = None,
    blacklist: set[str] | None = None,
) -> tuple[str, str]:
    """判定一条好友申请怎么处理。返回 (决定, 原因)。

    顺序：黑名单 → 白名单 → 等级 → 会员 → 老号（短号）→ 否则挂起待人工。
    """
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return DECISION_PENDING, "QQ 号无效"
    if blacklist and qq in blacklist:
        return DECISION_REJECT, "在黑名单里"
    if whitelist and qq in whitelist:
        return DECISION_ACCEPT, "在白名单里"

    level = extract_level(info)
    if level >= max(0, int(min_level)):
        return DECISION_ACCEPT, f"QQ 等级 {level} ≥ {min_level}"
    if accept_vip and is_vip(info):
        return DECISION_ACCEPT, "会员账号"
    max_len = max(0, int(max_qq_len))
    if max_len and len(qq) <= max_len:
        return DECISION_ACCEPT, f"老号（{len(qq)} 位）"
    return DECISION_PENDING, f"等级 {level} 偏低且非会员、非老号"


class DailyCounter:
    """按天计数的限额（用于限制一天最多自动通过多少个）。"""

    def __init__(self, limit: int = 5):
        self.limit = max(0, int(limit))
        self._date = ""
        self._count = 0

    def allow(self, today: str) -> bool:
        if self.limit <= 0:
            return True
        if today != self._date:
            self._date, self._count = today, 0
        return self._count < self.limit

    def record(self, today: str) -> None:
        if today != self._date:
            self._date, self._count = today, 0
        self._count += 1

    @property
    def count(self) -> int:
        return self._count


def is_friend_request(raw: dict) -> bool:
    """判断原始事件是否为「加好友」申请。"""
    if not isinstance(raw, dict):
        return False
    return str(raw.get("post_type") or "") == "request" and str(
        raw.get("request_type") or ""
    ) == "friend"


REQUEST_NOTICE_WORDS = (
    "请求添加你为好友",
    "请求加你为好友",
    "申请添加你为好友",
    "加好友请求",
    "好友申请",
)


def is_friend_request_notice(text: str) -> bool:
    """QQ 会把一部分好友申请降级成「可疑好友申请」，以私聊系统消息出现。

    这类申请没有 OneBot request 事件，只能从消息文本识别，再用 NapCat 的
    get_doubt_friends_add_request / set_doubt_friends_add_request 处理。
    """
    line = str(text or "").strip()
    return bool(line) and any(word in line for word in REQUEST_NOTICE_WORDS)


def find_doubt_request(items, qq: str) -> dict | None:
    """在可疑好友申请列表里按 QQ 号找对应条目（带 flag 才能同意）。"""
    for item in items or []:
        if isinstance(item, dict) and str(item.get("user_id") or "") == str(qq):
            return item
    return None


# ---------------- 主人裁决 ----------------

_APPROVE_WORDS = ("同意", "通过", "接受", "加上", "同意通过")
_REJECT_WORDS = ("拒绝", "驳回", "不加")
_OWNER_REPLY_RE = re.compile(r"^(" + "|".join(_APPROVE_WORDS + _REJECT_WORDS) + r")(\d{5,12})?$")


def parse_owner_reply(text: str) -> tuple[str, str | None] | None:
    """解析主人的裁决回复。

    「同意 123456」「拒绝456789」「同意」都行；不是裁决命令返回 None。
    返回 (动作, QQ号或 None)；QQ 号缺省时由调用方取最新一条挂起申请。
    """
    line = re.sub(r"[\s，。,、！!？?～～]+", "", str(text or ""))
    match = _OWNER_REPLY_RE.match(line)
    if not match:
        return None
    action = "accept" if match.group(1) in _APPROVE_WORDS else "reject"
    return action, match.group(2)


class PendingRequests:
    """等待主人裁决的好友申请：QQ → {flag, kind, ts}。

    flag 会过期（QQ 申请约 7 天失效），所以带 TTL 自动清理；容量封顶防刷。
    """

    def __init__(self, ttl_hours: int = 168, cap: int = 50):
        self.ttl_seconds = max(1, int(ttl_hours)) * 3600
        self.cap = max(1, int(cap))
        self._items: dict[str, dict] = {}
        self._seq = 0

    def _prune(self) -> None:
        now = time.time()
        self._items = {
            k: v for k, v in self._items.items() if now - v["ts"] < self.ttl_seconds
        }

    def add(self, qq: str, flag: str, kind: str) -> None:
        self._prune()
        while len(self._items) >= self.cap:
            self._items.pop(min(self._items, key=lambda k: self._items[k]["seq"]))
        self._seq += 1
        self._items[str(qq)] = {
            "flag": str(flag),
            "kind": kind,
            "ts": time.time(),
            "seq": self._seq,
        }

    def get(self, qq: str | None = None) -> tuple[str, dict] | None:
        """按 QQ 号取；qq 为空时取最新的一条。返回 (qq, item) 或 None。"""
        self._prune()
        if qq:
            qq = str(qq)
            return (qq, self._items[qq]) if qq in self._items else None
        if not self._items:
            return None
        latest = max(self._items, key=lambda k: self._items[k]["seq"])
        return latest, self._items[latest]

    def pop(self, qq: str) -> dict | None:
        self._prune()
        return self._items.pop(str(qq), None)

    def snapshot(self) -> dict:
        """导出可 JSON 化的挂起记录（重启后恢复）。"""
        self._prune()
        return {k: dict(v) for k, v in self._items.items()}

    def restore(self, data: dict) -> None:
        self._items = {}
        if isinstance(data, dict):
            for qq, item in data.items():
                if not isinstance(item, dict) or not item.get("flag"):
                    continue
                self._items[str(qq)] = {
                    "flag": str(item["flag"]),
                    "kind": str(item.get("kind") or "normal"),
                    "ts": float(item.get("ts") or 0),
                    "seq": int(item.get("seq") or 0),
                }
        self._seq = max((v["seq"] for v in self._items.values()), default=0)
        self._prune()

    def __len__(self) -> int:
        self._prune()
        return len(self._items)


def describe(info: dict) -> str:
    """给主人看的一句话摘要。"""
    if not isinstance(info, dict):
        return "信息未知"
    nickname = str(info.get("nickname") or "").strip() or "未知昵称"
    level = extract_level(info)
    parts = [nickname]
    if level:
        parts.append(f"{level} 级")
    if is_vip(info):
        parts.append("会员")
    return "，".join(parts)
