from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

from src.image_path import account_id_dir, image_rel_path

if TYPE_CHECKING:
    from src.config import AppConfig


logger = logging.getLogger(__name__)


def _search_image_key(obj):
    """递归查找飞书 post 图文混排结构里第一个 image_key。

    飞书 post 的 image_key 嵌在 content 数组的 ``{tag:"img"}`` 元素中；
    钉钉无此结构（图片用 mediaId= 查询串，已由上方正则捕获），递归不会误命中。
    """
    if isinstance(obj, dict):
        if obj.get("tag") == "img" and obj.get("image_key"):
            return obj["image_key"]
        for v in obj.values():
            r = _search_image_key(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _search_image_key(v)
            if r:
                return r
    return None


from src.poller_mixins_base import PollerMixinBase


class OcrMixin(PollerMixinBase):
    """MessagePoller 子系统萃取（mixin，经多继承组合回主类）。"""

    # ------------------------------------------------------------------
    # 图片存储路径（新结构：<platform>/<account_id>/<chat_id>/<file>）
    # ------------------------------------------------------------------
    def _image_account_id(self) -> str:
        """返回 account 段目录名，形如 ``feishu_cli_xxx`` / ``dingtalk_ding9888...``。

        优先用 ``resolve_account_id`` 解析出的稳定账号键（钉钉=corpId、飞书=appId、
        企微=配置 sha），与迁移脚本完全一致，保证跨账号物理隔离、不串图。
        解析失败（CLI 不可用）时退回 ``current_user_id`` 维度，避免塌缩到 unknown
        导致不同账号图片混到同一目录。
        """
        from src.memory.account_identity import resolve_account_id

        aid = resolve_account_id(self.platform_id)
        if aid.endswith(":unknown"):
            aid = f"{self.platform_id}:{self.current_user_id}"
        return account_id_dir(aid)

    def _image_storage(self, chat_id: str, filename: str) -> tuple[Path, str]:
        """生成新结构图片的「绝对路径」与「DB 相对路径」。

        ``chat_id`` 也走安全替换（钉钉 chat_id 是 base64，含 ``+``/``/``/``=``）。
        返回 ``(img_abs_path, rel_path)``，调用方直接写 ``img_abs_path``，
        DB 存 ``rel_path``，前端用 ``/api/image/<rel_path>`` 取图。
        """
        account_id = self._image_account_id()
        rel = image_rel_path(
            self.config.image_temp_dir,
            self.platform_id,
            account_id,
            chat_id,
            filename,
        )
        return Path(self.config.image_temp_dir).expanduser() / rel, rel

    @staticmethod
    def _extract_media_id(raw_content: str) -> str | None:
        """从消息 content 字段提取媒体 ID，兼容钉钉 / 飞书两种格式。

        钉钉格式：``mediaId=abc123``（查询串或嵌入 JSON）。
        飞书格式：``{"image_key": "img_xxxxxxx"}``（JSON 对象）。
        返回 media_id 字符串或 None。
        """
        if not raw_content:
            return None
        # 钉钉 mediaId= 格式。
        # mediaId 是查询串的首位键值，后面常跟 &text=用户说明 这类随图文字（caption），
        # &/? 是查询串键值分隔符；媒体 id 本身不会含这些字符，必须排除，否则会把
        # "&text=说明" 一并吞进 media_id，导致下游 download_media 拿到错误 id 而下载失败。
        m = re.search(r"mediaId=([^\s\)\]&?]+)", raw_content)
        if m:
            return m.group(1)
        # 飞书 image_key 格式（JSON 对象；也兼容 post 图文混排里嵌套的 image_key）
        try:
            c = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError) as _exc:
            logger.debug(f"_extract_media_id: swallowed exception: {_exc}")
            return None
        if isinstance(c, dict) and c.get("image_key"):
            return c["image_key"]
        # 飞书 post 图文混排：image_key 嵌在 content 数组的 {tag:"img"} 元素中，
        # 递归查找首个 image_key；钉钉无此结构，递归不会误命中。
        if isinstance(c, (dict, list)):
            found = _search_image_key(c)
            if found:
                return found
        return None

    def _submit_image_for_ocr(self, raw: dict, chat_id: str, chat_name: str,
                               fallback: str, msg_id: str) -> tuple[str, str]:
        """异步提交图片进行 OCR 处理，立即返回 (占位内容, 图片相对路径)。

        OCR 完成后通过回调更新消息内容 + 图片路径（供消息记录页缩略图）。

        关键修复：
        1. 同一条消息只提交一次 OCR（_ocr_futures 在途 + _ocr_results 已完成双重去重）
        2. OCR 完成后把图片相对路径持久化到数据库（之前被丢弃，缩略图功能失效）
        3. OCR 完成后从 _ocr_futures 移除，并对结果缓存设上限，避免无限增长
        """
        caption = self._extract_image_caption(raw)
        placeholder = f"{caption}\n[图片识别中...]" if caption else "[图片识别中...]"

        def ocr_task():
            try:
                result_content, rel_path = self._resolve_image_content(raw, chat_id, chat_name, fallback)
                with self._ocr_lock:
                    if result_content:
                        self._ocr_results[msg_id] = result_content
                    if rel_path:
                        self._ocr_image_paths[msg_id] = rel_path
                        # 把图片路径持久化到消息记录（消息记录页缩略图）
                        try:
                            self.store._message_repo.update_message_image_path(msg_id, rel_path)
                        except Exception as e:
                            logger.debug("[轮询器] 更新图片路径失败: %s", e)
                    # OCR 完成，移出在途字典（结果已落缓存，wait_for_ocr 仍能取到）
                    self._ocr_futures.pop(msg_id, None)
                    # 防缓存无限增长：超过阈值时淘汰最旧的一批
                    # 两个 dict 同步淘汰同一批 key，避免键集错位
                    if len(self._ocr_results) > 3000:
                        keys_to_evict = list(self._ocr_results.keys())[:500]
                        for k in keys_to_evict:
                            self._ocr_results.pop(k, None)
                            self._ocr_image_paths.pop(k, None)
                if result_content and "[图片识别中...]" not in result_content:
                    logger.info("[轮询器] OCR 完成，更新消息内容: %s", msg_id[:20])
                    self.store._message_repo.update_message_content(msg_id, result_content)
                return result_content
            except Exception as e:
                logger.warning("[轮询器] OCR 回调失败: %s", e)
                with self._ocr_lock:
                    self._ocr_futures.pop(msg_id, None)
                    self._ocr_results[msg_id] = ""  # 空结果防止无限重试
                return None

        # 【关键修复】检查与提交必须在同一把锁内，避免 TOCTOU 竞态：
        # 两个线程同时通过检查后会重复提交 OCR 任务。ocr_task 闭包定义可在锁外。
        try:
            with self._ocr_lock:
                if msg_id in self._ocr_futures or msg_id in self._ocr_results:
                    cached_path = self._ocr_image_paths.get(msg_id, "")
                    logger.debug("[轮询器] 图片消息 %s OCR 已在处理/已完成，跳过重复提交", msg_id[:20])
                    return placeholder, cached_path
                # 【诊断日志】仅在真正提交 OCR 任务时输出，避免已处理消息每轮刷屏
                if not caption:
                    logger.info(
                        "[轮询器][图片消息] caption为空——raw keys=%s, "
                        "msgType=%s, content前100字=%s, fallback前80字=%s",
                        list(raw.keys())[:20],
                        raw.get("msgType", ""),
                        (raw.get("content") or "")[:100],
                        fallback[:80] if fallback else "",
                    )
                # ThreadPoolExecutor worker 线程默认不继承 main thread 的 ContextVar，
                # 用 copy_context() 把当前平台上下文（含 with_platform/conv_conn 路由）
                # 复制到 worker，否则 ocr_task 内 update_message_* 走 conv_conn("") → 警告 + 空命名空间。
                import contextvars as _cv
                future = self._image_executor.submit(_cv.copy_context().run, ocr_task)
                self._ocr_futures[msg_id] = future
            return placeholder, ""
        except Exception as e:
            logger.warning("[轮询器] 提交 OCR 任务失败: %s", e)
            if caption:
                return f"{caption}\n[图片] (识别队列繁忙)", ""
            return "[图片] (识别队列繁忙)", ""

    def wait_for_ocr(self, msg_id: str, timeout: float = 10.0) -> str | None:
        """等待指定消息的 OCR 完成，返回识别结果。

        先查未来仍在运行中的 OCR，再查已完成结果缓存，双重保障。
        如果该消息没有在 OCR 队列中，返回 None。
        超时返回 None。
        """
        with self._ocr_lock:
            future = self._ocr_futures.get(msg_id)
        if future is not None:
            try:
                result = future.result(timeout=timeout)
                if result:
                    return result
            except Exception as e:
                logger.debug("[轮询器] OCR future 获取结果超时或失败: %s", e)
        # future 不存在或已完成：查持久结果缓存
        with self._ocr_lock:
            cached = self._ocr_results.get(msg_id)
            if cached:
                return cached
        return None

    def get_image_path(self, msg_id: str) -> str:
        """获取某消息 OCR 后的图片相对路径。

        供消息合并时把图片路径从「原始图片消息」带到「合并后的主消息」，
        避免图片+文字合并后路径落在被丢弃的图片 msg_id 行上、缩略图失效。
        """
        with self._ocr_lock:
            return self._ocr_image_paths.get(msg_id, "")

    def _resolve_image_content(self, raw: dict, chat_id: str, chat_name: str,
                                fallback: str) -> tuple[str, str]:
        """图片消息：下载并 OCR，持久化保存图片，返回 (可喂LLM的文字内容, 图片相对路径)。

        图片按聊天对象分目录存储：./data/tmp_images/<chat_name>/ocr_<msg_id>.png
        若消息同时附带文字说明（caption，如「帮我看下这个报错」），会与 OCR
        结果一起保留，避免「截图+一段文字」场景下用户写的文字被丢弃。任何失败都优雅降级。
        """
        caption = self._extract_image_caption(raw)
        raw_content = raw.get("content") or ""

        # 【兜底】如果 _extract_image_caption 未命中，但 fallback（_extract_content
        # 的返回值）包含非 mediaId 的实质文字，很可能是 DWS 把随图文字嵌在
        # content 字段的非预期位置。捞出来当 caption，避免用户文字被静默丢弃。
        if not caption and fallback:
            fb = fallback.strip()
            # 排除纯 mediaId 串、纯 JSON、纯 URL 等非用户文字
            if (len(fb) > 6
                    and not fb.startswith("mediaId=")
                    and not fb.startswith("{")
                    and not fb.startswith("http")
                    and "[图片]" not in fb
                    and not fb.startswith("*")):
                # 如果 fallback 含 mediaId= 则截取其前/后可能附带的文字
                if "mediaId=" in fb:
                    parts = re.split(r"[?&]mediaId=[^\s&]*", fb)
                    candidate = "".join(p.strip() for p in parts if p.strip())
                    if len(candidate) > 4:
                        caption = candidate
                        logger.info(
                            "[轮询器][caption-兜底] 从 fallback 提取到随图文字: %s",
                            caption[:80]
                        )
                elif any('\u4e00' <= c <= '\u9fff' for c in fb):
                    # 含中文且不是已知格式 → 直接当作用户文字
                    caption = fb
                    logger.info(
                        "[轮询器][caption-兜底-fallback] 使用 fallback 作为随图文字: %s",
                        caption[:80]
                    )

        media_id = self._extract_media_id(raw_content)
        if not media_id:
            if caption:
                return f"{caption}\n[图片] (未找到媒体ID，无法识别)", ""
            return "[图片] (未找到媒体ID，无法识别)", ""
        msg_id = raw.get("openMessageId") or raw.get("msgId") or ""
        conv_id = chat_id
        if not msg_id or not conv_id:
            if caption:
                return f"{caption}\n[图片] (缺少消息/会话标识，无法下载)", ""
            return "[图片] (缺少消息/会话标识，无法下载)", ""

        # 新结构：图片按 <platform>/<account_id>/<chat_id>/ 分目录隔离存储
        safe_id = re.sub(r"[^\w]", "_", media_id)[:60]
        filename = f"ocr_{safe_id}.png"
        img_path, rel_path = self._image_storage(chat_id, filename)

        try:
            img_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug("[轮询器] 创建图片目录失败: %s", e)

        try:
            self.dws.download_media(
                media_id=media_id,
                message_id=msg_id,
                conversation_id=conv_id,
                output_path=str(img_path),
            )
            if not img_path.exists() or img_path.stat().st_size == 0:
                logger.warning("[轮询器] 图片下载为空: %s", media_id[:20])
                if caption:
                    return f"{caption}\n[图片] (下载失败或为空)", ""
                return "[图片] (下载失败或为空)", ""
            ocr_text = self._ocr_image(str(img_path))
            if ocr_text:
                # 【OCR 后处理】去噪 + 格式整理，避免一长串脏内容干扰 LLM。
                # 处理后文本全链路统一使用（存库 / 展示 / 投喂 / 历史上下文）。
                try:
                    from src.tools.parse_document import post_process_ocr_text
                    ocr_text = post_process_ocr_text(ocr_text)
                except Exception as e:
                    logger.warning("[轮询器] OCR 后处理失败，回退原始文本: %s", e)
                if ocr_text:
                    # ★ 结构化包裹：用 <card title="图片内容"> 包住 OCR 文本，
                    # 前端 renderMsgContent 会走 _renderCardBody 把「标签：值」渲染成 KV 卡片。
                    # 历史消息的旧「【图片内容】\n...」格式前端也兼容（见 messages.js），
                    # 故此处新消息统一输出 card 结构，展示即整齐。
                    card_block = f'<card title="图片内容">\n{ocr_text}\n</card>'
                    if caption:
                        return f"{caption}\n{card_block}", rel_path
                    return card_block, rel_path
            if caption:
                return f"{caption}\n[图片 - 无法识别文字]", rel_path
            return "[图片 - 无法识别文字]", rel_path
        except Exception as e:
            logger.warning("[轮询器] 图片下载/OCR 失败: %s", e)
            if caption:
                return f"{caption}\n[图片] (识别失败)", ""
            return "[图片] (识别失败)", ""

    def _download_image_only(self, raw: dict, chat_id: str, chat_name: str,
                             fallback: str, msg_id: str) -> tuple[str, str]:
        """【我发送的图片】仅下载保存，不 OCR，用于对话框展示。

        与 _download_image_for_ocr 共用下载逻辑，但跳过识别步骤，直接把图片
        持久化到 data/tmp_images/<chat_name>/ 并返回相对路径，供前端对话框
        <img> 直接展示。带存在性去重：目标文件已存在且非空时直接复用，避免
        每轮轮询对同一张图重复下载。任何失败均优雅降级为占位内容。
        """
        caption = self._extract_image_caption(raw)
        raw_content = raw.get("content") or ""
        media_id = self._extract_media_id(raw_content)
        if not media_id:
            if caption:
                return f"{caption}\n[图片] (未找到媒体ID，无法保存)", ""
            return "[图片] (未找到媒体ID，无法保存)", ""
        conv_id = chat_id
        if not msg_id or not conv_id:
            if caption:
                return f"{caption}\n[图片] (缺少消息/会话标识，无法下载)", ""
            return "[图片] (缺少消息/会话标识，无法下载)", ""

        # 新结构：图片按 <platform>/<account_id>/<chat_id>/ 分目录隔离存储
        safe_id = re.sub(r"[^\w]", "_", media_id)[:60]
        filename = f"ocr_{safe_id}.png"
        img_path, rel_path = self._image_storage(chat_id, filename)

        try:
            img_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug("[轮询器] 创建自发送图片目录失败: %s", e)

        # 去重：文件已存在且非空，直接复用，避免重复下载
        if img_path.exists() and img_path.stat().st_size > 0:
            logger.debug("[轮询器] 自发送图片已存在，跳过下载: %s", rel_path)
            return (caption or "[图片]"), rel_path

        try:
            self.dws.download_media(
                media_id=media_id,
                message_id=msg_id,
                conversation_id=conv_id,
                output_path=str(img_path),
            )
            if not img_path.exists() or img_path.stat().st_size == 0:
                logger.warning("[轮询器] 自发送图片下载为空: %s", media_id[:20])
                if caption:
                    return f"{caption}\n[图片] (下载失败或为空)", ""
                return "[图片] (下载失败或为空)", ""
            if caption:
                return caption, rel_path
            return "[图片]", rel_path
        except Exception as e:
            logger.warning("[轮询器] 自发送图片下载失败: %s", e)
            if caption:
                return f"{caption}\n[图片] (下载失败)", ""
            return "[图片] (下载失败)", ""

    def _file_storage(self, chat_id: str, filename: str) -> tuple[Path, str]:
        """计算接收文件的本地存储路径（与图片同构：<platform>/<account>/<chat>/<name>）。

        落在 data/recv_files/ 下（image_temp_dir 的同级目录），与图片隔离，
        避免与 OCR 临时图混在一起。返回 (绝对路径, 相对路径)。
        """
        account_id = self._image_account_id()
        safe_chat = re.sub(r"[^\w]", "_", chat_id or "chat")[:80]
        rel = f"{self.platform_id}/{account_id}/{safe_chat}/{filename}"
        base = Path(self.config.image_temp_dir).expanduser().parent / "recv_files"
        return base / self.platform_id / account_id / safe_chat / filename, rel

    def _download_received_file(self, raw: dict, chat_id: str, chat_name: str,
                                msg_id: str, media_type: str) -> tuple[str, str]:
        """【接收文件闭环】把对方发来的 文件/语音/视频 下载到本地，供分身后继转发/回发。

        钉钉 file/voice/video 消息 content 是 JSON（含 filename/fileSize/mediaId 等），
        但 media_id 字段命名不统一（mediaId / fileId / file_id），这里做兼容提取。
        下载成功后返回 (本地绝对路径, 相对路径)；任何失败均优雅降级为 ("", "")，
        绝不因为下载失败而阻断正常收消息流程。
        """
        raw_content = raw.get("content") or ""
        media_id = self._extract_media_id(raw_content)
        # 文件类消息可能用 fileId / file_id 而非 mediaId
        if not media_id:
            try:
                c = json.loads(raw_content)
                if isinstance(c, dict):
                    media_id = c.get("mediaId") or c.get("fileId") or c.get("file_id")
            except Exception as _exc:
                logger.debug(f"_download_received_file: swallowed exception: {_exc}")
                pass
        if not media_id:
            return "", ""
        if not msg_id or not chat_id:
            return "", ""

        # 文件名：优先取消息里的 filename，否则按媒体类型生成默认名
        filename = None
        try:
            c = json.loads(raw_content)
            if isinstance(c, dict):
                filename = c.get("filename") or c.get("fileName")
        except Exception as _exc:
            logger.debug(f"_download_received_file: swallowed exception: {_exc}")
            pass
        if not filename:
            ext = {"voice": "amr", "video": "mp4", "file": "bin"}.get(media_type, "bin")
            filename = f"{media_type}_{re.sub(r'[^\w]', '_', str(media_id))[:40]}.{ext}"
        else:
            # 防目录穿越：仅保留 basename
            filename = os.path.basename(filename)
            if not filename:
                filename = "attachment.bin"
        safe_name = re.sub(r"[^\w.\-]", "_", filename)[:120]

        out_path, rel = self._file_storage(chat_id, safe_name)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug("[轮询器] 创建接收文件目录失败: %s", e)

        # 去重：文件已存在且非空，直接复用，避免每轮轮询重复下载
        if out_path.exists() and out_path.stat().st_size > 0:
            logger.debug("[轮询器] 接收文件已存在，跳过下载: %s", rel)
            return str(out_path), rel

        try:
            self.dws.download_media(
                media_id=media_id,
                message_id=msg_id,
                conversation_id=chat_id,
                output_path=str(out_path),
            )
            if not out_path.exists() or out_path.stat().st_size == 0:
                logger.warning("[轮询器] 接收文件下载为空: %s", media_id[:20])
                return "", ""
            logger.info("[轮询器] 接收%s已下载到本地: %s", media_type, rel)
            return str(out_path), rel
        except Exception as e:
            logger.warning("[轮询器] 接收文件下载失败: %s", e)
            return "", ""

    @staticmethod
    def _extract_card_image_keys(content: str) -> list[str]:
        """从消息内容中提取所有飞书卡片图片键（img_key / file_key / IMG_KEY）。

        覆盖格式（飞书消息卡片 interactive / post 图文混排等场景）：
          - 🖼️ Image(img_key:img_v3_xxx)
          - ✨(img_key:img_v3_xxx)
          - Image(file_key:file_v3_xxx)
          - Image(IMG_KEY)
        返回按出现顺序去重的 key 列表；空内容返回 []。
        """
        if not content:
            return []
        seen: set[str] = set()
        result: list[str] = []
        # 匹配 "(img_key|file_key|IMG_KEY):xxxx" 形式
        for m in re.finditer(r"\((?:img_key|file_key|IMG_KEY):([^\s)]+)\)", content):
            k = m.group(1)
            if k and k not in seen:
                seen.add(k)
                result.append(k)
        return result

    def _download_card_images(self, content: str, chat_id: str, chat_name: str,
                              msg_id: str) -> dict[str, str]:
        """下载飞书消息卡片里的所有图片，返回 ``{media_key: rel_path}`` 映射。

        适用场景：``interactive`` / ``post`` 类型的飞书消息（如飞书智能助手、飞行社 bot
        发的卡片），content 里嵌了多个 ``🖼️ Image(img_key:...)`` 标签。_resolve_image_content
        只处理单图且只在 msg_type=='image' 时触发，对这种「卡片内嵌多图」场景不覆盖，
        导致这些图从来没被下载到本地、UI 上只能显示"图片"两字。

        本方法做三件事：
        1) 用正则扫 content 提取所有 (img_key|file_key|IMG_KEY:xxx)；
        2) 逐个调 ``self.dws.download_media`` 落到 ``data/tmp_images/<chat_name>/``；
        3) 去重 + 文件已存在复用 + 单个失败不阻断其他图，返回成功下载的 {key: rel_path} 映射。

        错误隔离：单张图下载失败不抛异常，只在 logger 记录，确保其他图能继续下。
        返回空 dict 表示一张都没下到（content 里没图 / 全部失败）。
        """
        keys = self._extract_card_image_keys(content)
        if not keys:
            return {}

        mapping: dict[str, str] = {}
        for key in keys:
            # file_key 须用 .bin 后缀避免 lark-cli 误判（飞书 media_key 前缀约定
            # 见 _infer_resource_type）；img_v3_* 默认按 image 落 .png
            safe_id = re.sub(r"[^\w]", "_", key)[:80]
            if key.startswith("img_"):
                filename = f"card_{safe_id}.png"
            elif key.startswith("file_"):
                filename = f"card_{safe_id}.bin"
            else:
                filename = f"card_{safe_id}.bin"
            # 新结构：<platform>/<account_id>/<chat_id>/<file>
            img_path, rel_path = self._image_storage(chat_id, filename)
            try:
                img_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as _exc:
                logger.warning(f"_download_card_images: swallowed exception: {_exc}")
                pass

            # 去重：文件已存在且非空直接复用
            try:
                if img_path.exists() and img_path.stat().st_size > 0:
                    mapping[key] = rel_path
                    continue
            except OSError as _exc:
                logger.debug(f"_download_card_images: swallowed exception: {_exc}")
                pass

            try:
                self.dws.download_media(
                    media_id=key,
                    message_id=msg_id,
                    conversation_id=chat_id,
                    output_path=str(img_path),
                )
                if img_path.exists() and img_path.stat().st_size > 0:
                    mapping[key] = rel_path
                else:
                    logger.warning(
                        "[轮询器] 卡片图片下载为空: msg=%s key=%s",
                        msg_id[:20], key[:30],
                    )
            except Exception as e:
                logger.warning(
                    "[轮询器] 卡片图片下载失败: msg=%s key=%s err=%s",
                    msg_id[:20], key[:30], e,
                )
                # 清理可能写出的 0 字节文件
                try:
                    if img_path.exists() and img_path.stat().st_size == 0:
                        img_path.unlink()
                except OSError as _exc:
                    logger.debug(f"_download_card_images: swallowed exception: {_exc}")
                    pass
        return mapping

    def _ocr_image(self, path) -> str:
        """调用文档解析器的 OCR 能力（懒加载 + 实例缓存复用，避免每张图都重载 OCR 模型）。"""
        try:
            from src.tools.parse_document import DocumentParser
        except Exception as e:
            logger.warning("[轮询器] OCR 模块加载失败: %s", e)
            return ""
        # 缓存 DocumentParser 实例：首次创建后复用，避免每张图都重新加载模型（数秒延迟）。
        # 双重检查锁保证多线程（_image_executor 线程池）下仅初始化一次。
        if self._doc_parser is None:
            with self._ocr_cache_lock:
                if self._doc_parser is None:
                    try:
                        # poller 只有 PollerConfig，但 DocumentParser 仅存储 config
                        # （内部不读取具体字段），运行时兼容；此处 cast 以满足静态类型。
                        self._doc_parser = DocumentParser(cast("AppConfig", self.config))
                        logger.info("[轮询器] OCR 引擎实例已初始化并缓存复用")
                    except Exception as e:
                        logger.warning("[轮询器] OCR 解析器初始化失败: %s", e)
                        return ""
        parser = self._doc_parser
        if not getattr(parser, "_ocr_available", False):
            logger.warning("[轮询器] OCR 依赖不可用（未安装 pytesseract/Pillow）")
            return ""
        try:
            with self._ocr_call_lock:
                return parser.ocr_image(str(path))
        except Exception as e:
            logger.warning("[轮询器] OCR 执行失败: %s", e)
            return ""
