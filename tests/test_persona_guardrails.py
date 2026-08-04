"""PII 脱敏 / 不当内容护栏 / few-shot 推荐多样性 的回归测试。

覆盖 #20（PII 与不当内容护栏）与 #22（推荐样例多样性增强）的核心逻辑，
防止后续重构回退。可在现有 CI 的 pytest 任务中自动收集执行。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

# 保证仓库根目录在 sys.path（pytest 默认 rootdir 插入在不同布局下不一定生效）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.memory.sqlite_store import (
    SQLiteStore,
    _redact_pii,
    _is_inappropriate,
    _has_residual_pii,
)


class TestPIIRedaction(unittest.TestCase):
    def test_phone_redacted(self):
        self.assertIn("[已脱敏]", _redact_pii("我的手机号是13800138000，有空联系"))
        # 真实独立手机号整体被替换
        out = _redact_pii("打我电话13800138000谢谢")
        self.assertEqual(out, "打我电话[已脱敏]谢谢")

    def test_idcard_full_match(self):
        # 18 位身份证应作为整体脱敏，不应残留尾部数字
        out = _redact_pii("身份证440301199001011234请核对")
        self.assertEqual(out, "身份证[已脱敏]请核对")

    def test_idcard_15digit(self):
        out = _redact_pii("旧证440301900101123")
        self.assertEqual(out, "旧证[已脱敏]")

    def test_email_redacted(self):
        out = _redact_pii("邮箱alice.wang@corp.com.cn收到没")
        self.assertEqual(out, "邮箱[已脱敏]收到没")

    def test_address_redacted(self):
        out = _redact_pii("我住北京市海淀区中关村大街12号3栋502室")
        self.assertIn("[已脱敏]", out)

    def test_address_fine_compound(self):
        # 更细地址：楼栋/单元/室号组合与省+市+区完整链应被整体脱敏
        self.assertIn("[已脱敏]", _redact_pii("他在3号楼2单元501室等我"))
        self.assertIn("[已脱敏]", _redact_pii("广东省深圳市南山区科技园路1号腾讯大厦"))

    def test_residual_check(self):
        # 正常文本无残留
        self.assertFalse(_has_residual_pii("明天开会讨论一下方案吧"))
        # 脱敏后占位符本身不触发残留（幂等）
        self.assertFalse(_has_residual_pii("打我电话[已脱敏]谢谢"))
        # 未脱敏的 PII 应被检出
        self.assertTrue(_has_residual_pii("联系13800138000"))

    def test_no_false_positive(self):
        # 「1号选手」「门店3」等不应被误判为手机/身份证
        self.assertNotIn("[已脱敏]", _redact_pii("1号选手加油，门店3营业中"))
        self.assertNotIn("[已脱敏]", _redact_pii("今天气温23度，第15名出线"))

    def test_empty_input(self):
        self.assertEqual(_redact_pii(""), "")
        self.assertEqual(_redact_pii("   "), "   ")


class TestInappropriateGuard(unittest.TestCase):
    def test_detects_profanity(self):
        self.assertTrue(_is_inappropriate("这家伙真傻逼，去死吧"))

    def test_clean_passes(self):
        self.assertFalse(_is_inappropriate("明天开会讨论一下方案吧"))
        self.assertFalse(_is_inappropriate(""))
        self.assertFalse(_is_inappropriate("1号选手加油"))


def _seed_messages(store: SQLiteStore, rows):
    """插入若干 messages（id 自增，created_at 必填）。

    注意：few-shot 推荐读取的是按平台隔离的会话库 conv_conn（见
    baseline_repo.recommend_few_shot_pairs 的 _cc），而非主库 store.conn；
    故此处必须写入 conv_conn，否则种子数据对推荐不可见、用例全空。
    """
    conn = store._baseline_repo._cc("")
    cur = conn.cursor()
    cur.execute("DELETE FROM messages")
    conn.commit()
    for r in rows:
        # (chat_id, content, sender_name, is_bot, role, msg_type)
        cur.execute(
            "INSERT INTO messages (chat_id, content, sender_name, is_bot, role, msg_type, created_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'))",
            r,
        )
    conn.commit()


class TestFewShotDiversity(unittest.TestCase):
    OWNER = "OWNER"

    def _build_store(self):
        db = tempfile.mktemp(suffix=".db")
        store = SQLiteStore(db)
        self.addCleanup(lambda: (store.conn.close(), os.remove(db)))
        return store

    def test_exclude_adopted(self):
        store = self._build_store()
        rows = []
        # 大量雷同「在吗/在的，有什么事吗」配对（回复须 >=8 字才过质量门）
        for i in range(12):
            rows.append((f"c{i}", "在吗", "同事", 0, "user", "text"))
            rows.append((f"c{i}", "在的，有什么事吗", self.OWNER, 0, "assistant", "text"))
        _seed_messages(store, rows)
        adopted = [{"user": "在吗", "assistant": "在的，有什么事吗"}]
        pairs = store._baseline_repo.recommend_few_shot_pairs(self.OWNER, limit=6, exclude=adopted)
        self.assertEqual(pairs, [])  # 全部被已采纳排除
        # 不带 exclude 时应能拿出该对
        pairs2 = store._baseline_repo.recommend_few_shot_pairs(self.OWNER, limit=6)
        self.assertTrue(any(p["assistant"] == "在的，有什么事吗" for p in pairs2))

    def test_diversity_length_and_topic(self):
        store = self._build_store()
        themes = [
            ("在吗", "在的，你说一下吧"),
            ("收到", "收到，我看一下先"),
            ("谢谢", "好的，没问题的事"),
            ("几点", "可以，那就这么定"),
            ("合同", "嗯，我待会处理下"),
            ("价格", "行，我转给同事了"),
            ("进度", "稍等，我查一下哈"),
            ("吃饭", "好嘞，马上安排上"),
            ("文件", "对的，正是这个意"),
            ("测试", "明白，我记下来了"),
        ]
        # 中长回复（21~60 字，落在 m 桶），与上方短回复（8~20，s 桶）形成长度多样性
        mediums = [
            "在的，有什么事您尽管说，我这边收到后会尽快帮你安排处理",
            "收到，我马上处理一下这个事情，处理完第一时间同步给你",
            "不客气，这是我应该做的，以后还有类似的事直接找我就好",
            "下午三点我们在三楼会议室碰一下吧，记得带上那份材料",
            "合同已经盖章了，今天下午走顺丰寄出，单号出来发你",
            "这个报价我觉得可以接受，你跟对方确认下交付时间就行",
            "进度正常，预计周五可以按时交付，到时候提前一天发你",
            "走，一起去吃个饭呗，楼下那家新开的火锅评价不错",
            "文件我已经放到群里了，请大家查收一下，看完群里说",
            "测试用例全部跑过了没有问题，覆盖率也达标可以上线",
        ]
        rows = []
        for i, (u, sh) in enumerate(themes):
            cid = f"c{i}"
            rows.append((cid, u, "同事", 0, "user", "text"))
            rows.append((cid, sh, self.OWNER, 0, "assistant", "text"))
            rows.append((cid, u + "详细说", "同事", 0, "user", "text"))
            rows.append((cid, mediums[i], self.OWNER, 0, "assistant", "text"))
        _seed_messages(store, rows)
        pairs = store._baseline_repo.recommend_few_shot_pairs(self.OWNER, limit=8)
        self.assertTrue(len(pairs) >= 2)
        buckets = {"s": 0, "m": 0, "l": 0}
        for p in pairs:
            n = len(p["assistant"])
            b = "s" if n <= 20 else ("m" if n <= 60 else "l")
            buckets[b] += 1
        # 短/中长度桶都应出现（不会被单一长度桶垄断）
        self.assertTrue(buckets["s"] > 0 and buckets["m"] > 0)

    def test_near_duplicate_collapsed(self):
        store = self._build_store()
        rows = []
        # 两条几乎相同的长回复（仅标点/少量字差异）→ 近似去重应只留一条
        for i in range(5):
            rows.append((f"c{i}", "方案发你", "同事", 0, "user", "text"))
            rows.append((f"c{i}", "好的，方案已经发到你邮箱请查收，有问题随时说", self.OWNER, 0, "assistant", "text"))
        _seed_messages(store, rows)
        pairs = store._baseline_repo.recommend_few_shot_pairs(self.OWNER, limit=6)
        # 近似重复的回复只会出现一次（subject 不同但 reply 文本相同/近似）
        replies = [p["assistant"] for p in pairs]
        self.assertEqual(len(replies), len(set(replies)))


class TestStyleProfileVersions(unittest.TestCase):
    """#3 画像版本管理：覆盖前归档、trigger 溯源、回滚不丢链、字段剥离。"""

    def _store(self):
        import tempfile, os
        d = tempfile.mkdtemp()
        return SQLiteStore(os.path.join(d, "v.db"))

    def test_archive_on_overwrite_and_trigger_trace(self):
        s = self._store()
        p1 = {"prompt": "P1", "confidence": "low", "cleaned_count": 10, "updated_at": "2026-01-01T00:00:00"}
        p2 = {"prompt": "P2", "confidence": "high", "cleaned_count": 200, "updated_at": "2026-06-01T00:00:00"}
        s._memory_ops_repo.save_style_profile(p1, "manual")   # 首次：无历史可归档
        s._memory_ops_repo.save_style_profile(p2, "auto")      # 二次：归档 P1，其 trigger 应为 manual
        vers = s._memory_ops_repo.list_style_profile_versions()
        self.assertEqual(len(vers), 1)
        self.assertEqual(vers[0]["trigger"], "manual")   # 旧版 P1 的来源被正确溯源
        self.assertEqual(vers[0]["confidence"], "low")
        # 当前画像干净、不含内部溯源字段
        cur = s._memory_ops_repo.get_style_profile()
        self.assertEqual(cur.get("prompt"), "P2")
        self.assertNotIn("_save_trigger", cur)

    def test_rollback_preserves_chain(self):
        s = self._store()
        p1 = {"prompt": "P1", "confidence": "low", "cleaned_count": 10, "updated_at": "2026-01-01T00:00:00"}
        p2 = {"prompt": "P2", "confidence": "high", "cleaned_count": 200, "updated_at": "2026-06-01T00:00:00"}
        p3 = {"prompt": "P3", "confidence": "medium", "cleaned_count": 80, "updated_at": "2026-07-01T00:00:00"}
        s._memory_ops_repo.save_style_profile(p1, "manual")
        s._memory_ops_repo.save_style_profile(p2, "auto")
        s._memory_ops_repo.save_style_profile(p3, "manual")
        # 回滚到最早的 P1
        v1 = [v for v in s._memory_ops_repo.list_style_profile_versions() if v["version_no"] == 1][0]
        self.assertTrue(s._memory_ops_repo.rollback_style_profile(v1["id"]))
        self.assertEqual(s._memory_ops_repo.get_style_profile().get("prompt"), "P1")
        # 历史链增长且当前被归档（trigger 取自身来源 manual）
        vers = s._memory_ops_repo.list_style_profile_versions()
        self.assertEqual(len(vers), 3)
        self.assertEqual(vers[0]["trigger"], "manual")  # 最新一条是被回滚掉的 P3（manual）

    def test_missing_version_returns_none(self):
        s = self._store()
        self.assertIsNone(s._memory_ops_repo.get_style_profile_version(999))
        self.assertFalse(s._memory_ops_repo.rollback_style_profile(999))


if __name__ == "__main__":
    unittest.main()
