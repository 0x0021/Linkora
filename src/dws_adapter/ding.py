"""DwsAdapter DING 能力 mixin（个人强提醒触达）。拆分自 dws_adapter.py。"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

import logging

logger = logging.getLogger(__name__)


class DwsAdapterDingMixin(DwsAdapterBase):
    def ding_send_personal(self, *, users: list[str], content: str,
                           remind_type: str = "app",
                           uuid: str | None = None) -> dict:
        """以本人身份向指定人发送 DING 强提醒（应用内 / 短信 / 电话）。

        封装 ``dws ding +send-personal --users <openDingtalkId,...> --content <文本>
        --type <remindType> [--uuid <幂等键>]``。

        DING 是比普通消息更强的触达通道，适合紧急告警 / 待办逾期提醒等场景。
        写操作：强制真实执行（不受全局 dry_run 影响），并带 ``--yes`` 通过确认闸门。

        Args:
            users: 接收人 openDingTalkId 列表（必填）
            content: DING 内容（必填）
            remind_type: 提醒方式 remindType，默认 ``"app"``（应用内）；
                可选 ``"sms"``（短信）/ ``"call"``（电话）
            uuid: 幂等键，可选；重复发送同 uuid 服务端幂等，避免告警风暴重复触达

        Returns:
            dws 返回的 result dict
        """
        if not users:
            raise ValueError("ding_send_personal 需提供至少一个接收人 openDingTalkId")
        if not content:
            raise ValueError("ding_send_personal 需提供 content")

        args = [
            "ding", "+send-personal",
            "--users", ",".join(users),
            "--content", content,
            "--type", remind_type,
        ]
        if uuid:
            args.extend(["--uuid", uuid])

        logger.debug("[DWS] 发送个人 DING 给 %d 人 (type=%s)", len(users), remind_type)
        # 写操作：force_no_dry_run 保证真实发出（_build_command 已自动补 -y）
        return self.run(args, operation="ding_send_personal", force_no_dry_run=True)
