"""Bot 好友申请：自动通过「看起来是真人的」好友申请，其余交主人裁决。

判定在 library.evaluate 里：等级够高 / 有会员 / 是老号（短 QQ 号）三者之一就通过，
黑名单拒绝，其余挂起并把对方信息发给主人——主人回复「同意 QQ号」或
「拒绝 QQ号」后再真正处理申请（主人只回「同意/拒绝」则取最新一条）。
每天有自动通过上限，防止被批量小号刷；无论自动还是手动，处理后都会私聊告知主人。

实现注意：AstrBot 会把 OneBot 的 request 事件转成 FRIEND_MESSAGE 交给插件，
原始事件在 event.message_obj.raw_message 里，靠 post_type/request_type 区分。
QQ 还会把一部分申请降级成「可疑好友申请」：没有 request 事件，只有一句
私聊系统消息，靠 NapCat 扩展接口 get/set_doubt_friends_add_request 处理
（该接口只能同意、不能拒绝）。没有加好友前 cautiously 聊几句是允许的，
所以识别不出的系统消息会放行给 LLM。
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig

from .library import (
    DECISION_ACCEPT,
    DECISION_REJECT,
    DailyCounter,
    PendingRequests,
    describe,
    evaluate,
    extract_level,
    find_doubt_request,
    is_friend_request,
    is_friend_request_notice,
    is_vip,
    parse_owner_reply,
    parse_qq_list,
)

NAME = "astrbot_plugin_snow_friend"


class SnowFriend(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self._counter = DailyCounter(int(config.get("max_accept_per_day", 5) or 5))
        self._pending = PendingRequests(
            ttl_hours=int(config.get("review_ttl_hours", 168) or 168)
        )

    # ---------------- 配置 ----------------
    def _enabled(self) -> bool:
        return bool(self.conf.get("enabled", True))

    def _cfg(self, key, default):
        return self.conf.get(key, default)

    def _pending_path(self) -> Path:
        try:
            base = Path(str(StarTools.get_data_dir(NAME)))
        except Exception:
            base = Path("data") / "plugin_data" / NAME
        base.mkdir(parents=True, exist_ok=True)
        return base / "pending_reviews.json"

    def _load_pending(self) -> None:
        try:
            data = json.loads(self._pending_path().read_text(encoding="utf-8"))
        except Exception:
            data = {}
        self._pending.restore(data if isinstance(data, dict) else {})
        if len(self._pending):
            logger.info(f"[{NAME}] 恢复了 {len(self._pending)} 条待裁决好友申请")

    def _save_pending(self) -> None:
        path = self._pending_path()
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(self._pending.snapshot(), ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning(f"[{NAME}] 保存待裁决申请失败：{exc}")

    def _owner_umo(self) -> str:
        configured = str(self._cfg("owner_session", "") or "").strip()
        if configured:
            return configured
        try:
            cfg = self.context.get_config()
            admins = cfg.get("admins_id") or []
            platform_id = ""
            for p in cfg.get("platform", []) or []:
                if isinstance(p, dict) and p.get("type") == "aiocqhttp":
                    platform_id = str(p.get("id") or "")
                    break
            if admins and platform_id:
                return f"{platform_id}:FriendMessage:{admins[0]}"
        except Exception:
            pass
        return ""

    # ---------------- 协议端 ----------------
    def _client(self, event: AstrMessageEvent):
        bot = getattr(event, "bot", None)
        if bot is not None:
            return bot
        try:
            for inst in self.context.platform_manager.get_insts():
                if hasattr(inst, "get_client"):
                    return inst.get_client()
        except Exception:
            pass
        return None

    async def _stranger_info(self, client, qq: str) -> dict:
        """查申请者资料（等级、会员、昵称）。失败返回空 dict，不阻塞判定。"""
        try:
            data = await client.call_action("get_stranger_info", user_id=int(qq))
        except Exception as exc:
            logger.info(f"[{NAME}] 查询 {qq} 资料失败：{exc}")
            return {}
        if not isinstance(data, dict):
            return {}
        level_keys = sorted(
            k for k in data
            if k.lower() in ("qqlevel", "qq_level", "level", "vip_level", "vipflag", "vip_flag")
        )
        logger.info(
            f"[{NAME}] {qq} 资料：昵称={data.get('nickname') or '未知'} "
            f"等级={extract_level(data)} 会员={is_vip(data)} 相关字段={level_keys}"
        )
        return data

    @staticmethod
    async def _set_request(client, flag: str, approve: bool, comment: str = "") -> bool:
        try:
            await client.call_action(
                "set_friend_add_request", flag=flag, approve=bool(approve), remark=comment
            )
            return True
        except Exception as exc:
            logger.warning(f"[{NAME}] 处理好友申请失败：{exc}")
            return False

    # ---------------- 主流程 ----------------
    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE, priority=(1 << 31) - 1
    )
    async def on_friend_request(self, event: AstrMessageEvent):
        """拦截加好友申请：判定资格后自动同意/拒绝/挂起。"""
        if not self._enabled():
            return
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if not is_friend_request(raw):
            return
        # 这是申请事件，不是消息：处理完必须停掉，别让空消息进 LLM
        event.stop_event()

        qq = str(raw.get("user_id") or "").strip()
        if not qq:
            sender = getattr(message_obj, "sender", None)
            qq = str(getattr(sender, "user_id", "") or "").strip()
        flag = str(raw.get("flag") or "")
        comment = str(raw.get("comment") or "").strip()[:100]
        if not qq or not flag:
            logger.warning(f"[{NAME}] 好友申请缺少 user_id/flag，跳过：{raw}")
            return

        client = self._client(event)
        if client is None:
            logger.warning(f"[{NAME}] 没有可用的协议端，好友申请 {qq} 未处理")
            return

        info = await self._stranger_info(client, qq)
        decision, reason = self._evaluate(qq, info)
        today = date.today().isoformat()

        if decision == DECISION_ACCEPT:
            if not self._counter.allow(today):
                logger.info(
                    f"[{NAME}] 今日自动通过已达上限（{self._counter.limit}），"
                    f"{qq} 转入待处理（{reason}）"
                )
                decision = "pending"
                reason = "今日自动通过额度已用完"
            else:
                ok = await self._set_request(client, flag, True)
                if ok:
                    self._counter.record(today)
                    logger.info(f"[{NAME}] 已同意好友申请 {qq}（{reason}）")
                    await self._notify_owner(
                        f"同意了好友申请：{qq}（{describe(info)}，{reason}）"
                        + (f"\n附言：{comment}" if comment else "")
                    )
                    return
                logger.warning(f"[{NAME}] 同意 {qq} 失败，转人工处理")
                reason = "自动同意失败（申请可能已过期）"

        if decision == DECISION_REJECT and bool(self._cfg("reject_blacklisted", True)):
            ok = await self._set_request(
                client, flag, False, str(self._cfg("reject_comment", "") or "")
            )
            logger.info(f"[{NAME}] 已拒绝好友申请 {qq}（{reason}）" + ("" if ok else "（拒绝失败）"))
            return

        await self._hold_for_review(qq, flag, "normal", info, reason, comment)
        logger.info(f"[{NAME}] 好友申请 {qq} 等主人裁决：{reason}（{describe(info)}）")

    def _evaluate(self, qq: str, info: dict):
        return evaluate(
            qq,
            info=info,
            min_level=int(self._cfg("min_qq_level", 16)),
            accept_vip=bool(self._cfg("accept_vip", True)),
            max_qq_len=int(self._cfg("max_qq_len", 9)),
            whitelist=parse_qq_list(self._cfg("whitelist", "")),
            blacklist=parse_qq_list(self._cfg("blacklist", "")),
        )

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE, priority=(1 << 31) - 1
    )
    async def on_doubt_friend_request(self, event: AstrMessageEvent):
        """处理 QQ 降级成「可疑好友申请」的请求。

        这类申请没有 OneBot request 事件和 flag，只会以私聊系统消息
        「请求添加你为好友」出现；NapCat 提供了可疑申请列表和专用处理接口，
        但该接口只能同意、不能拒绝，所以拒绝只能在 QQ 里手动做。
        处理完不拦截这条消息——跟还没加上好友的人警惕地聊几句是允许的。
        """
        if not self._enabled():
            return
        text = str(event.message_str or "").strip()
        if not is_friend_request_notice(text):
            return
        qq = str(event.get_sender_id() or "").strip()
        if not qq:
            return
        client = self._client(event)
        if client is None:
            return
        try:
            items = await client.call_action("get_doubt_friends_add_request", count=50)
        except Exception as exc:
            logger.info(f"[{NAME}] 读取可疑好友申请失败：{exc}")
            return
        item = find_doubt_request(items, qq)
        if not item:
            # 列表里没有 TA：可能是普通申请事件（另一条 handler 处理），或已处理。
            return
        flag = str(item.get("flag") or "")
        if not flag:
            logger.warning(f"[{NAME}] 可疑好友申请 {qq} 缺少 flag，无法处理")
            return
        info = await self._stranger_info(client, qq)
        decision, reason = self._evaluate(qq, info)
        today = date.today().isoformat()
        if decision == DECISION_ACCEPT and self._counter.allow(today):
            try:
                await client.call_action(
                    "set_doubt_friends_add_request", flag=flag, approve=True
                )
            except Exception as exc:
                logger.warning(f"[{NAME}] 同意可疑好友申请 {qq} 失败：{exc}")
                reason = "自动同意失败（flag 可能已过期）"
                await self._hold_for_review(qq, flag, "doubt", info, reason)
                return
            self._counter.record(today)
            logger.info(f"[{NAME}] 已同意可疑好友申请 {qq}（{reason}）")
            await self._notify_owner(
                f"同意了好友申请：{qq}（{describe(info)}，{reason}）"
            )
            return
        if decision == DECISION_ACCEPT:
            reason = "今日自动通过额度已用完"
        await self._hold_for_review(qq, flag, "doubt", info, reason)

    async def _hold_for_review(
        self, qq: str, flag: str, kind: str, info: dict, reason: str, comment: str = ""
    ) -> None:
        """自动判定未通过：把对方信息发给主人，等「同意/拒绝 QQ号」的裁决。"""
        if not bool(self._cfg("review_enabled", True)):
            logger.info(f"[{NAME}] 好友申请 {qq} 保持待处理（未开转发）：{reason}")
            return
        self._pending.add(qq, flag, kind)
        self._save_pending()
        how = (
            f"回复「同意 {qq}」通过（拒绝要在 QQ 里手动处理，7 天内有效）。"
            if kind == "doubt"
            else f"回复「同意 {qq}」通过，或「拒绝 {qq}」拒绝（7 天内有效）。"
        )
        await self._notify_owner(
            f"收到好友申请：{qq}（{describe(info)}）\n"
            f"没有自动通过：{reason}"
            + (f"\n附言：{comment}" if comment else "")
            + f"\n{how}"
        )

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """裁决人必须是主人/管理员（网页测试台视为主人自己）。"""
        if str(event.get_platform_name() or "") == "webchat":
            return True
        owner = str(self._cfg("owner_session", "") or "").strip()
        if owner and str(event.unified_msg_origin or "") == owner:
            return True
        qq = str(event.get_sender_id() or "")
        try:
            admins = self.context.get_config().get("admins_id") or []
            if qq and str(qq) in {str(a) for a in admins}:
                return True
        except Exception:
            pass
        try:
            if callable(getattr(event, "is_admin", None)) and event.is_admin():
                return True
        except Exception:
            pass
        return False

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE, priority=(1 << 31) - 2
    )
    async def on_owner_review(self, event: AstrMessageEvent):
        """主人回复「同意/拒绝 [QQ号]」裁决挂起的好友申请。"""
        parsed = parse_owner_reply(str(event.message_str or ""))
        if not parsed:
            return
        # 只在 QQ 私聊里执行：网页测试台没有 OneBot 协议端，做不了加好友
        if str(event.get_platform_name() or "") != "aiocqhttp":
            return
        if not self._is_admin(event):
            return
        action, qq_arg = parsed
        if not qq_arg:
            # 只回「同意/拒绝」不带号码时不当命令，避免和正常聊天混淆
            return
        event.stop_event()
        client = self._client(event)
        if client is None:
            return
        hit = self._pending.get(qq_arg)
        if not hit:
            await self._notify_owner("没有找到对应的好友申请，可能已过期或已处理。")
            return
        qq, item = hit
        flag, kind = item["flag"], item["kind"]
        approve = action == "accept"

        if kind == "doubt" and not approve:
            # NapCat 的可疑申请接口强制同意，拒绝做不了
            self._pending.pop(qq)
            self._save_pending()
            await self._notify_owner(
                f"可疑申请 {qq} 只能通过接口同意；拒绝请在 QQ 里手动处理。"
            )
            return
        if kind == "normal":
            ok = await self._set_request(
                client, flag, approve,
                "" if approve else str(self._cfg("reject_comment", "") or ""),
            )
        else:
            try:
                await client.call_action(
                    "set_doubt_friends_add_request", flag=flag, approve=True
                )
                ok = True
            except Exception as exc:
                logger.warning(f"[{NAME}] 同意可疑申请 {qq} 失败：{exc}")
                ok = False
        self._pending.pop(qq)
        self._save_pending()
        if not ok:
            await self._notify_owner(
                f"处理 {qq} 失败：申请的 flag 可能已过期，让对方重新申请一次。"
            )
            return
        if approve:
            logger.info(f"[{NAME}] 主人裁决通过好友申请 {qq}")
            await self._notify_owner(f"已同意 {qq} 的好友申请。")
        else:
            logger.info(f"[{NAME}] 主人裁决拒绝好友申请 {qq}")
            await self._notify_owner(f"已拒绝 {qq} 的好友申请。")

    async def _notify_owner(self, text: str) -> None:
        if not bool(self._cfg("notify_owner", True)):
            return
        umo = self._owner_umo()
        if not umo:
            return
        try:
            delivered = await self.context.send_message(
                umo, MessageChain(chain=[Plain(text)])
            )
            if delivered is False:
                logger.info(f"[{NAME}] 通知未送达（QQ 可能未登录）：{umo}")
        except Exception as exc:
            logger.warning(f"[{NAME}] 通知主人失败：{exc}")

    # ---------------- 状态 ----------------
    async def initialize(self) -> None:
        self._load_pending()
        logger.info(
            f"[{NAME}] 已启用，判定线：等级≥{self._cfg('min_qq_level', 16)}、"
            f"会员={'算' if self._cfg('accept_vip', True) else '不算'}、"
            f"老号≤{self._cfg('max_qq_len', 9)}位、每日上限 {self._counter.limit}"
        )
