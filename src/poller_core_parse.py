from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.models import Message
from src.poller_utils import is_read_receipt_content

logger = logging.getLogger(__name__)

# OA 卡片深链/字段里的审批实例 ID 形态（钉钉多版本兼容）：
#   procInstId=xxx / processInstanceId":"xxx / procInsId=xxx
#   URL 编码形态：%22processInstanceId%22%3A%22xxx%22 / procInstId%2Fxxx（path 风格）
#   process_instance_id=xxx（下划线风格）/ ProcInstId=xxx（大小写兼容）
# 加固点：
#   - (?i) 忽略字段名大小写，兼容 ProcInstId / PROCESSINSTANCEID 等写法
#   - (?<![A-Za-z]) 仅排除「字母前缀」的标识符子串误匹配（如 originProcInstId）；
#     故意放行 digit 前缀——URL 编码形态 %22processInstanceId 的字段名前恰是 %22 的末位
#     数字 2，若连同 digit 一起排除会误杀真实场景
#   - 分隔符覆盖 " : = 空格 / 反斜杠 及其 URL 编码(%22/%3A/%3D/%2F)
#   - 实例 ID 至少 6 位（部分老实例为纯数字短编号），仅允许安全字符 [0-9A-Za-z_\-]
_OA_INSTANCE_ID_RE = re.compile(
    r"(?i)(?<![A-Za-z])"
    r"(?:procInstId|procInsId|processInstanceId|process_instance_id)"
    r"(?:%22|%3A|%3D|%2F|[\"':=/\\\s])*"
    r"([0-9A-Za-z_\-]{6,})"
)


def _extract_oa_instance_id(content_obj) -> str:
    """从 OA 卡片解析后的 content 对象里尽力提取审批实例 ID。

    钉钉转发的 OA 卡片不在 head/body 里显式给 instanceId，但 message_url /
    pc_message_url 深链参数（procInstId=xxx）通常带有。这里把整个对象序列化后
    正则搜索，多字段名兼容；找不到返回空串（调用方按无 ID 处理，不影响主流程）。
    """
    try:
        import json as _json
        blob = _json.dumps(content_obj, ensure_ascii=False)
    except (TypeError, ValueError) as _exc:
        logger.debug(f"_extract_oa_instance_id: swallowed exception: {_exc}")
        return ""
    m = _OA_INSTANCE_ID_RE.search(blob)
    return m.group(1) if m else ""


def _post_contains_image(post: dict) -> bool:
    """飞书 post 图文混排消息是否含图片。

    post 结构：``{"content": [[{tag:"text"...}, {tag:"img", image_key:"img_xxx"}]]}``。
    仅当存在 ``{tag:"img", image_key:...}`` 元素时才算图片消息，否则按纯文本处理。
    """
    blocks = post.get("content") or []
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, list):
            continue
        for el in block:
            if isinstance(el, dict) and el.get("tag") == "img" and el.get("image_key"):
                return True
    return False


def _extract_post_text_and_img(post: dict) -> tuple[str, bool]:
    """提取飞书 post 图文混排的纯文字与是否含图。

    返回 ``(拼接后的文字, 是否含图片)``。文字来自 ``{tag:"text"}`` 的 ``text`` 字段，
    图片仅做标记（不在此处下载，由 _raw_to_message 走 image 分支处理）。
    """
    texts: list[str] = []
    has_img = False
    blocks = post.get("content") or []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, list):
                continue
            for el in block:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag == "text":
                    t = (el.get("text") or "").strip()
                    if t:
                        texts.append(t)
                elif tag == "img":
                    has_img = True
    return " ".join(texts).strip(), has_img


class ParseMixin:
    """MessagePoller 子系统萃取（mixin，经多继承组合回主类）。"""

    def _parse_timestamp(self, ts_str: str) -> datetime:
        """解析钉钉消息时间戳为本地 naive datetime。

        钉钉可能返回多种格式：
          - "%Y-%m-%d %H:%M:%S"（无时区，按本地处理）
          - ISO 8601 无时区后缀（如 "2026-07-11T13:00:00"，按本地处理）
          - ISO 8601 带 UTC/offset（如 "2026-07-11T05:00:00Z" 或 "...+08:00"）
        为消除 naive/aware 混用导致的时区错位（M3 修复），统一收敛为本地 naive：
          - aware（带时区）→ 转本地时区后去掉 tzinfo；
          - naive（无时区）→ 视为本地，原样返回。
        任何解析失败均回退到 datetime.now()（本地），保证不抛异常。
        """
        local_tz = datetime.now().astimezone().tzinfo
        # 先试钉钉常见格式
        try:
            return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            logger.debug("[轮询器] 时间戳格式不匹配 strptime，尝试 ISO 8601: %s", ts_str[:40])
        # 再试 ISO 8601（兼容带 Z / +offset 的 aware 与无时区 naive）
        try:
            dt = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            logger.debug("[轮询器] 时间戳解析失败，回退当前时间: %s", ts_str[:40])
            return datetime.now()
        if dt.tzinfo is not None:
            # aware：转换到本地时区并去掉时区信息，与全链路 naive 本地时间保持一致
            return dt.astimezone(local_tz).replace(tzinfo=None)
        return dt

    def _effective_skip_types(self) -> set:
        """实际生效的跳过类型集合。OCR 启用时 image 不再被跳过（改由 OCR 转为文字）。
        P2-G：优雅回退类型（语音/视频）不硬跳过，需送达 main 走引导回复。"""
        skip = set(self.config.skip_msg_types) - set(
            getattr(self.config, "graceful_fallback_msg_types", []))
        if self.config.image_ocr_enabled:
            skip.discard("image")
        return skip

    def _detect_msg_type(self, raw: dict) -> str:
        """检测消息类型：text, system, app, file, image, voice, video, link, oa, markdown, call, edit, recall, read_receipt。"""
        # 1. 系统消息判断（先检查明确的系统标识，再检查 sender_id）
        sender = raw.get("sender") or raw.get("senderName") or ""
        
        # 明确的系统标识（优先级最高）
        if sender in ("系统", "System"):
            return "system"
        if self._is_system_sender(sender):
            return "system"
        
        # sender_id 为空时，不立即判定为 system——某些消息（如外部好友图片消息）可能没有 sender_id
        # 但有 sender_name，可以从其他字段推断类型
        sender_id = raw.get("senderOpenDingTalkId") or raw.get("senderId") or ""
        if not sender_id:
            # 如果有明确的 sender_name（真人名字），先不标记为 system，继续推断类型
            if sender and sender not in ("系统", "System"):
                pass
            else:
                return "system"

        # 2. 检查 msgType 字段（dws 可能直接提供）
        msg_type = raw.get("msgType", "") or raw.get("messageType", "")
        if msg_type:
            mt = msg_type.lower()
            # 飞书图文混排（post）：content 为二维数组 [[{tag:text/img}...]]。
            # 含图片则按 image 处理（走下载/OCR 分支），否则退化成纯 text，
            # 避免原样归为 "post" 既无法下载图片、又把嵌套 JSON 泄进历史/LLM。
            if mt == "post":
                content = raw.get("content") or ""
                if "{" in content:
                    try:
                        import json as _json
                        _c = _json.loads(content)
                        if isinstance(_c, dict) and _post_contains_image(_c):
                            return "image"
                    except (_json.JSONDecodeError, ValueError, TypeError) as _exc:
                        logger.debug(f"_detect_msg_type: swallowed exception: {_exc}")
                        pass
                return "text"
            type_map = {
                "text": "text",
                "image": "image",
                "voice": "voice",
                "video": "video",
                "file": "file",
                "link": "link",
                "oa": "oa",
                "markdown": "markdown",
                "action_card": "app",
                "card": "app",
                "rich": "app",
                "call": "call",
                "voip": "call",
                "audio_call": "call",
                "video_call": "call",
                "edit": "edit",
                "recall": "recall",
            }
            resolved = type_map.get(mt, mt)
            # 通话/编辑/撤回等系统通知：dws 可能把 msgType 标为 text（典型如钉钉
            # "[语音通话] 通话时长 XX秒"），但内容含明确系统通知关键词。这类消息
            # 不应触发 bot 回复，故对 text/markdown 等纯文本类型优先按关键词重分类，
            # 交由 skip_msg_types 过滤（call/edit/recall 均在默认跳过列表）。
            if resolved in ("text", "markdown", "unknown", ""):
                _kw = self._classify_by_content_keywords(raw.get("content") or "")
                if _kw:
                    return _kw
            return resolved

        # 2.5 通过 content 前缀判断图片消息（mediaId=xxx 查询串格式，非 JSON）
        if not msg_type:
            raw_content = raw.get("content") or ""
            if raw_content.startswith("mediaId=") or "mediaId=" in raw_content:
                return "image"

        # 3. 通过 content 结构判断
        content = raw.get("content") or ""
        if content.startswith("https://") or content.startswith("http://"):
            return "link"
        if "{" in content and "}" in content:
            try:
                import json
                c = json.loads(content)
                if isinstance(c, dict):
                    if "markdown" in c:
                        return "markdown"
                    if "single_title" in c or "single_url" in c:
                        return "link"
                    if "filename" in c or "fileSize" in c:
                        return "file"
                    if "image" in c or "media_id" in c or "picUrl" in c:
                        return "image"
                    if "voice" in c or "duration" in c:
                        return "voice"
                    if "video" in c:
                        return "video"
                    if "oa" in c or "head" in c or "body" in c:
                        return "oa"
                    if "call" in c or "voip" in c or "duration" in c:
                        return "call"
                    return "app"
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug("[轮询器] 消息类型 JSON 解析失败: %s", e)

        # 4. 通过内容关键词判断通话/编辑/撤回等系统通知（content 无 JSON 结构时）
        _kw = self._classify_by_content_keywords(content)
        if _kw:
            return _kw

        # 5. 默认 text
        return "text"

    @staticmethod
    def _classify_by_content_keywords(content: str) -> str | None:
        """按内容关键词识别系统通知类型（call / edit / recall）。

        dws 可能将「通话结束/通话时长」「消息已编辑」「消息已撤回」等系统通知的
        msgType 标为 text，但内容含明确关键词。这些通知不应触发 bot 回复，故优先
        识别为对应类型，交由 skip_msg_types 过滤（call/edit/recall 均已在默认跳过列表）。

        命中返回类型字符串，未命中返回 None。
        """
        c = (content or "").lower()
        call_keywords = ["通话结束", "通话时长", "语音通话", "视频通话", "通话记录", "通话详情"]
        for kw in call_keywords:
            if kw in c:
                return "call"
        edit_keywords = ["消息已编辑", "编辑消息", "msgedited", "edited"]
        recall_keywords = ["消息已撤回", "撤回消息", "msgrecalled", "recalled", "recall"]
        for kw in edit_keywords:
            if kw in c:
                return "edit"
        for kw in recall_keywords:
            if kw in c:
                return "recall"
        # 已读回执类系统通知（机器生成，锚定方括号/回执/英文，避免误伤真人消息）
        if is_read_receipt_content(content):
            return "read_receipt"
        return None

    @staticmethod
    def _extract_rich_text(blob: str) -> tuple[str, bool]:
        """从钉钉富文本 items JSON 中提取纯文本。

        处理形如 [{"text":{"items":[{"data":{"text":"..."},"type":"text"}]}}] 的
        多样式富文本卡片（钉钉 "在吗" 自动回复、多样式提示等常见结构）。
        可能包含多段拼接的 JSON（整体 json.loads 会失败），故用 raw_decode 逐段解析。
        返回 (提取到的文本, 是否含图片)。
        """
        import json as _json
        import re as _re
        texts: list[str] = []
        has_img = [False]

        def walk(o):
            if isinstance(o, dict):
                if o.get("type") == "text" and isinstance(o.get("data"), dict):
                    t = o["data"].get("text", "")
                    if t:
                        texts.append(t)
                if o.get("type") == "image" or o.get("preview") is True:
                    has_img[0] = True
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        dec = _json.JSONDecoder()
        n = len(blob)
        # 顶层的 [ 是数组开始符，不能当空白跳过——先尝试整体解析数组/对象
        try:
            walk(_json.loads(blob))
        except Exception as e:
            logger.debug("[轮询器] JSON 整体解析失败，尝试逐段拼接: %s", e)
            # 多段拼接 JSON（如 [{...}]\n{...}，整体解析失败）时用 raw_decode 逐段解析。
            # 只跳过分隔符/空白；[ 与 { 是 JSON 起始符，不能跳过。
            pos = 0
            while pos < n:
                if blob[pos] in ", \n\t\r":
                    pos += 1
                    continue
                try:
                    obj, end = dec.raw_decode(blob, pos)
                    walk(obj)
                    pos = end
                except Exception:
                    logger.debug("[轮询器] raw_decode 跳过异常增量: pos %d/%d", pos, n)
                    pos += 1
        # 去掉纯表情/方向占位符（如 [向右]）与多余空白
        text = "".join(texts)
        text = _re.sub(r"\[向[上下左右]\]", "", text)
        return text.strip(), has_img[0]

    def _extract_content(self, raw: dict) -> str:
        """从原始消息中提取可读的文本内容。"""
        content = raw.get("content") or ""
        if not content:
            return ""

        # 【钉钉富文本/深链注入清洗】处理 "* 仅你和对方可见\n[{...items...}]" 这类
        # 未被下方 json.loads 捕获的泄露格式（以 * 开头、[ 数组、多段 JSON）。
        # 避免原样吐出大段原始 JSON 污染历史并営给 LLM。
        _stripped = content.lstrip()
        if ("minSupportVersion" in content or '"preview"' in content
                or _stripped.startswith("* 仅") or "仅你和对方可见" in content
                or ('"data"' in content and '"type"' in content and "[{" in content)
                or "dingtalk://" in content):
            import re as _re0
            s = content.strip()
            s = _re0.sub(r"^\*?\s*(该消息仅自己可见|仅你和对方可见|仅自己可见)\s*\n?", "", s).strip()
            idx = s.find("[{")
            if idx < 0:
                _m = _re0.search(r'\{"(text|preview|image)"', s)
                idx = _m.start() if _m else -1
            if idx >= 0:
                prefix = s[:idx].strip()
                txt, img = self._extract_rich_text(s[idx:])
                if txt:
                    return (prefix + " " + txt).strip() if prefix else txt
                if img:
                    return (prefix + " [图片]").strip() if prefix else "[图片]"
                if prefix:
                    return prefix
            # 无富文本结构，至少清洗 dingtalk:// 深链后继续
            content = _re0.sub(r"\[?dingtalk://[^\]\s]+\]?", "", s).strip()

        # 尝试解析 JSON 格式的富媒体消息
        if "{" in content and "}" in content:
            try:
                import json
                c = json.loads(content)
                if isinstance(c, dict):
                    # 飞书 post 图文混排：content 是二维数组 [[{tag:text/img}...]]，
                    # 与钉钉 image/file 的 content(字符串) 区分；优先提取文字并标 [图片]，
                    # 避免落入下方 `if "content" in c: return c["content"]` 返回原始 JSON 数组。
                    if "content" in c and isinstance(c["content"], list):
                        texts, has_img = _extract_post_text_and_img(c)
                        if texts or has_img:
                            base = texts
                            if has_img:
                                base = (base + " [图片]").strip() if base else "[图片]"
                            return base
                    # markdown 消息
                    if "markdown" in c and isinstance(c["markdown"], dict):
                        return c["markdown"].get("text", content)
                    # 链接消息
                    if "text" in c and isinstance(c["text"], str):
                        return c["text"]
                    if "content" in c and isinstance(c["content"], str):
                        return c["content"]
                    # 文件消息
                    if "filename" in c:
                        base = f"[文件] {c.get('filename', '')}"
                        file_id = c.get("fileId", "")
                        if file_id:
                            base += f"\nfileId: {file_id}"
                        return base
                    # 图片消息
                    if "picUrl" in c:
                        return f"[图片] {c.get('picUrl', '')}"
                    if "image_key" in c:
                        return "[图片]"
                    if "media_id" in c:
                        return "[图片/媒体]"
                    # 语音消息
                    if "voice" in c:
                        return "[语音消息]"
                    # OA 消息
                    if "oa" in c and isinstance(c["oa"], dict):
                        oa = c["oa"]
                        # 钉钉 OA 卡片 head/body 可能为显式 null（None）。
                        # 用 or {} 兜底：.get 默认值仅在 key 缺失时生效，
                        # key 存在但值为 None 时仍返回 None → 下一跳 .get 崩。
                        head = oa.get("head") or {}
                        body = oa.get("body") or {}
                        # head.text / body.form 同样可能为显式 null：
                        # 用 or 兜底（默认值对显式 None 不生效）。
                        title = head.get("text") or ""
                        forms = body.get("form") or []
                        form_text = "\n".join([f"{f.get('key')}: {f.get('value')}" for f in forms])
                        text = f"[OA审批] {title}\n{form_text}".strip()
                        # 尽力提取审批实例 ID（藏在 message_url/pc_message_url 等深链参数里），
                        # 便于下游「审批转交」等工具直接定位实例、免按标题反查
                        inst = _extract_oa_instance_id(c)
                        if inst:
                            text += f"\ninstanceId: {inst}"
                        return text
            # 防御性兜底：OA 卡片字段缺失/类型异常（AttributeError/TypeError）也
            # 必须降级为原始 content，绝不能逃逸中断整批轮询（否则该卡片留在时间窗内
            # 会每轮复现，导致全员停止回复）。
            except (json.JSONDecodeError, ValueError, AttributeError, TypeError) as e:
                logger.debug("[轮询器] OA 卡片解析失败（已降级为原始正文）: %s", e)

        # 兜底：清洗正文里嵌入的 dingtalk:// 深链与飞书 clickable 容器标签，
        # 保留内部纯文本（避免泄漏原始 <clickable> 标签到历史/LLM）。
        # 注意：不要清洗 🖼️ Image(img_key:...) / ✨(img_key:...) 这类 image 标签——
        # image_key 是后续下载与 LLM 语义理解的唯一信号，丢了无法恢复。
        # 跨 app 资源（飞书智能助手、飞行社等 bot 发的图）受飞书平台隔离限制，
        # linkora 的 app 凭证无法下载；这部分由前端 Pass 0 渲染为带说明的占位符。
        import re as _re1
        content = _re1.sub(r"\[?dingtalk://[^\]\s]+\]?", "", content)
        content = _re1.sub(r"<clickable[^>]*>(.*?)</clickable>", r"\1", content, flags=_re1.DOTALL)
        content = _re1.sub(r"</?clickable[^>]*>", "", content)
        content = content.strip()
        return content

    def _raw_to_message(self, raw: dict, chat_id: str, chat_type: str,
                        chat_name: Optional[str]) -> Message:
        msg_id = raw.get("openMessageId") or raw.get("msgId") or ""
        sender = raw.get("sender") or raw.get("senderName") or ""
        sender_id = raw.get("senderOpenDingTalkId") or raw.get("senderId") or ""
        content = self._extract_content(raw)
        ts_str = raw.get("createTime") or raw.get("timestamp") or ""
        ts = self._parse_timestamp(ts_str) if ts_str else datetime.now()
        msg_type = self._detect_msg_type(raw)
        # 图片消息处理：
        # - 他人发送 + 启用 OCR：下载并 OCR，让 LLM 读取截图内容
        # - 我（主人）发送的：不 OCR，但下载保存，便于对话框里展示
        # - 他人图片 + OCR 关闭：不下载不识别（保持空，下游按 OCR 关闭逻辑跳过）
        image_path = ""
        # 历史回填模式（_skip_ocr=True）：完全跳过图片下载 / OCR / 卡片图下载，只存文本。
        # 这样「全部历史」同步不触发重型图片管线，速度显著提升；图片可在日常轮询时补齐。
        if not self._skip_ocr and msg_type == "image":
            is_self = (
                (sender_id and (sender_id == self.current_user_id or sender_id == self.current_user_user_id))
                or (not sender_id and sender and self.current_user_name
                    and sender.strip() == self.current_user_name.strip())
            )
            # chat_name 为空时用发送者名代替，避免下载到「未知」目录
            effective_name = chat_name or sender or "图片"
            if is_self:
                # 【我发送的】跳过 OCR，仅下载落盘供前端对话框展示
                content, image_path = self._download_image_only(raw, chat_id, effective_name, content, msg_id)
            elif self.config.image_ocr_enabled:
                content, image_path = self._submit_image_for_ocr(raw, chat_id, effective_name, content, msg_id)
        elif not self._skip_ocr and msg_type in ("interactive", "post") and content:
            # 飞书消息卡片 / 图文混排：content 里嵌了多个 🖼️ Image(img_key:...) /
            # ✨(img_key:...) 标签。走 _download_card_images 逐个下载到本地，
            # 把 {key: rel_path} 写为 JSON 到 image_path（前端按 JSON 解析后渲染真图）。
            # 智能助手/飞行社等 bot 发的图也走这条：之前从未下载到，UI 只能显示
            # "图片"两字；现在补齐下载通道。
            effective_name = chat_name or sender or "卡片"
            mapping = self._download_card_images(content, chat_id, effective_name, msg_id)
            if mapping:
                image_path = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
        elif not self._skip_ocr and msg_type in ("file", "voice", "video"):
            # 【接收文件闭环】把对方发来的 文件/语音/视频 下载到本地，并在正文追加
            # 本地绝对路径，使分身之后能用 send_message(file_path=...) 转发或回发该文件，
            # 支撑「把刚才那个合同发我」「转发给XX」类自动回复。下载失败优雅降级（不阻塞收消息）。
            local_path, _rel = self._download_received_file(
                raw, chat_id, chat_name or sender or "文件", msg_id, msg_type)
            if local_path:
                content = f"{content}\n[本地文件] {local_path}"

        # 把已下载媒体的本地绝对路径追加到正文，便于分身引用/转发
        # （图片路径来自 image_path 相对路径，需还原为绝对路径）
        if image_path:
            try:
                abs_img = str(Path(self.config.image_temp_dir).expanduser() / image_path)
                content = f"{content}\n[本地图片] {abs_img}"
            except Exception as _exc:
                logger.debug(f"_raw_to_message: swallowed exception: {_exc}")
                pass
        return Message(
            msg_id=msg_id,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_name,
            sender_id=sender_id,
            sender_name=sender,
            content=content,
            msg_type=msg_type,
            timestamp=ts,
            raw=raw,
            image_path=image_path,
        )

    def _extract_image_caption(self, raw: dict) -> str:
        """从图片消息体中提取随图发送的文字说明（caption）。

        优先取消息顶层 `text` 字段（钉钉常把图片的文字说明放这里），其次取
        content JSON 内独立的文字字段。仅当消息确为图片（含 mediaId/picUrl）
        且另有独立文字时才视为 caption，避免把原始 `mediaId=xxx` 串误当文字。

        覆盖的 DWS 入站格式（按优先级）：
          A) raw["text"] — 顶层文字（最常见）
          B) raw["content"] 为 JSON → 内层 text/content/title/description/summary
          C) raw["content"] 为查询串 "mediaId=xxx&text=用户文字" → 提取 text 参数
          D) raw["title"] / raw["description"] / raw["summary"]
        """
        import json
        from urllib.parse import parse_qs

        # ---- A) 顶层 text 字段（排除本身就是 mediaId 串的情况）----
        top = (raw.get("text") or "").strip()
        if top and not top.startswith("mediaId="):
            return top

        content = (raw.get("content") or "").strip()
        if not content:
            return ""

        # ---- B) content 为 JSON 对象时，取内层文字字段 ----
        if content.startswith("{"):
            try:
                c = json.loads(content)
            except Exception as e:
                logger.debug("[轮询器] caption JSON 解析失败: %s", e)
            else:
                if isinstance(c, dict):
                    if not (c.get("mediaId") or c.get("picUrl")
                            or "mediaId=" in content):
                        return ""
                    for key in ("text", "content", "title",
                                "description", "summary", "body"):
                        v = c.get(key)
                        if isinstance(v, str) and v.strip():
                            return v.strip()

        # ---- C) content 为查询串格式（mediaId=xxx&text=用户文字）----
        if "mediaId=" in content and ("&" in content or "?" in content):
            try:
                params = parse_qs(content, keep_blank_values=True)
                for key in ("text", "content", "title",
                            "description", "caption"):
                    vals = params.get(key)
                    if vals and isinstance(vals, list) and vals[0].strip():
                        cap = vals[0].strip()
                        if not cap.startswith("mediaId="):
                            logger.info(
                                "[轮询器] 从 content 查询串提取到随图文字(%s): %s",
                                key, cap[:80]
                            )
                            return cap
            except Exception as e:
                logger.debug("[轮询器] caption 查询串解析失败: %s", e)

        # ---- D) 其他顶层的可能文字字段 ----
        for field in ("title", "description", "summary"):
            v = raw.get(field)
            if isinstance(v, str) and v.strip() and len(v.strip()) > 2:
                return v.strip()

        # 【诊断日志】帮助排查未覆盖的 DWS 格式——仅当消息看起来像
        # 「图片+用户文字」（content 含非 mediaId 的实质内容）时才打
        _diag = content[:200] if len(content) > 20 else ""
        if _diag and not _diag.startswith("mediaId="):
            logger.debug(
                "[轮询器][caption-未命中] 未从图片消息中提取到随图文字。"
                " raw keys=%s, content前120字=%s",
                list(raw.keys())[:15], _diag[:120],
            )
        return ""

    def _merge_consecutive_messages(self, messages: list[Message],
                                     window_seconds: int = 60) -> list[Message]:
        """合并同一人在短时间窗口内的连续消息。

        优化：先按 (chat_id, sender) 分组，避免跨会话排序导致同一会话消息被拆散；
        合并时过滤掉纯礼貌/感谢/确认/结束语消息，避免这类消息干扰业务处理。
        """
        if not messages:
            return messages

        # 按 (chat_id, sender_id|sender_name) 分组，避免跨会话消息互相干扰
        from collections import defaultdict
        groups = defaultdict(list)
        for msg in messages:
            key = (msg.chat_id, msg.sender_id or msg.sender_name)
            groups[key].append(msg)

        merged = []
        for key, group in groups.items():
            group.sort(key=lambda m: m.timestamp)
            current_group: list[Message] = []

            for msg in group:
                if not current_group:
                    current_group.append(msg)
                    continue

                last = current_group[-1]
                time_diff = (msg.timestamp - last.timestamp).total_seconds()
                same_sender = (msg.sender_id == last.sender_id
                               or msg.sender_name == last.sender_name)
                same_chat = msg.chat_id == last.chat_id

                if same_chat and same_sender and time_diff <= window_seconds:
                    current_group.append(msg)
                else:
                    merged.append(self._combine_message_group(current_group))
                    current_group = [msg]

            if current_group:
                merged.append(self._combine_message_group(current_group))

        return merged

    def _is_polite_message(self, content: str) -> bool:
        """判断消息是否是「纯」礼貌/感谢/确认/结束语消息或纯表情。

        仅当整条消息除礼貌词/表情外不含其它实质内容时才视为纯礼貌消息，
        从而在消息合并阶段将其过滤。若消息混合了业务内容
        （如「收到，帮我导出报表」「谢谢，已处理」），应保留业务部分，
        不能因含礼貌词或表情就整条丢弃。
        """
        if not content:
            return False

        text = content.strip()
        if not text:
            return False

        remaining = text

        # 1) 移除 Unicode emoji（如 👍、🙏、😄 等）
        remaining = re.sub(
            r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF]",
            "", remaining
        )
        # 2) 移除 IM 方括号表情（如 [赞] [强] [OK] [握手] [玫瑰] [抱拳] [咖啡]）
        remaining = re.sub(r"\[[^\[\]]+\]", "", remaining)

        # 礼貌/确认/感谢/结束语词（含大小写变体）
        polite_words = [
            "谢谢", "感谢", "辛苦了", "辛苦", "谢了", "多谢", "感恩",
            "收到", "好的", "明白", "知道了", "了解", "清楚了", "没问题",
            "OK", "ok", "再见", "拜拜", "晚安", "先忙", "收工", "下班",
        ]
        # 句末语气词、称呼、填充词：即使跟在礼貌词后，也应视作「无业务内容」，
        # 否则「好的 明白了」去掉「明白」后残留「了」、「谢谢老板」残留「老板」会误判为业务。
        fillers = [
            "了", "呢", "吧", "啊", "嘛", "吗", "哦", "嗯", "额", "诶", "哈",
            "呀", "啦", "老板", "老大", "亲", "哥", "姐", "总", "师傅",
            "老师", "同学", "朋友",
        ]

        for kw in polite_words + fillers:
            # 大小写不敏感移除（覆盖 OK/ok/Ok），中文无影响
            remaining = re.sub(re.escape(kw), "", remaining, flags=re.IGNORECASE)

        # 移除所有空白与常见标点后若为空，说明整条消息仅由礼貌词/表情/填充词构成
        stripped = re.sub(r"[\s，。！？、,.!?~～\-—()（）“”\"'」』:：；;]", "", remaining)
        return stripped == ""

    def _wrap_image_block(self, content: str) -> str:
        """给合并组中的图片 OCR 内容加显式分隔块，与用户手写文字区分。

        钉钉「文本+图+文本」「图+文本+文本」等混合场景下，图片识别出的文字
        容易与用户手打的多段文字混淆，用区块线明确边界，提升 LLM 理解准确率。
        """
        if not content:
            return content
        return (
            "———— 图片识别内容 ————\n"
            f"{content}\n"
            "———— 图片识别内容结束 ————"
        )

    def _combine_message_group(self, group: list[Message]) -> Message:
        """将一组消息合并为一条消息，内容按时间顺序用换行连接。

        优化：过滤掉纯礼貌/感谢消息，只保留业务内容；组内含图片时给其 OCR
        内容加显式分隔块，避免与用户手写文字混淆；若组内同时含图片与其它类型
        消息（文本+图+文本 / 图+文本+文本），合并后类型标为 `mixed`，防止下游
        把「图开头的混合消息」误判为纯图片。
        如果一组消息全部是礼貌消息，则返回第一条（会被 rule_engine 过滤）。
        """
        if len(group) == 1:
            return group[0]

        first = group[0]
        types = {m.msg_type for m in group}
        has_image = "image" in types
        has_other = any(t != "image" for t in types)

        # 过滤掉纯礼貌/感谢消息，只保留业务内容
        filtered_contents = []
        polite_count = 0
        for m in group:
            if not m.content:
                continue
            if self._is_polite_message(m.content):
                polite_count += 1
                continue
            if m.msg_type == "image":
                filtered_contents.append(self._wrap_image_block(m.content))
            else:
                filtered_contents.append(m.content)

        # 如果全部是礼貌消息，保留第一条（让 rule_engine 处理）
        if not filtered_contents:
            logger.debug("[合并] 全部是礼貌消息，保留第一条")
            return group[0]

        # 如果有部分礼貌消息被过滤，记录日志
        if polite_count > 0:
            logger.debug("[合并] 过滤了 %d 条礼貌消息，保留 %d 条业务消息", polite_count, len(filtered_contents))

        combined_content = "\n".join(filtered_contents)
        # 混合了图片与其它类型的组标记为 mixed，避免下游误判为纯图片
        merged_type = "mixed" if (has_image and has_other) else first.msg_type

        last = group[-1]
        return Message(
            msg_id=last.msg_id,
            chat_id=first.chat_id,
            chat_type=first.chat_type,
            chat_name=first.chat_name,
            sender_id=first.sender_id,
            sender_name=first.sender_name,
            content=combined_content,
            msg_type=merged_type,
            timestamp=last.timestamp,
            raw={"merged": True, "count": len(group), "filtered_polite": polite_count,
                 "original_ids": [m.msg_id for m in group], "merged_type": merged_type},
        )

    def _is_at_me(self, raw: dict) -> bool:
        """检查群消息是否@了当前用户。"""
        at_users = raw.get("atUsers") or raw.get("atUserIds") or []
        if isinstance(at_users, list):
            for u in at_users:
                if isinstance(u, dict):
                    uid = u.get("userId") or u.get("openDingTalkId") or u.get("dingtalkId") or ""
                    if uid and (
                        uid == self.current_user_id
                        or uid == self.current_user_user_id
                    ):
                        return True
                elif isinstance(u, str) and (
                    u == self.current_user_id
                    or u == self.current_user_user_id
                ):
                    return True
        # 同时检查消息内容中是否包含@用户名
        content = raw.get("content") or ""
        if self.current_user_name and f"@{self.current_user_name}" in content:
            return True
        return False
