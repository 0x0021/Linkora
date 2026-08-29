"""日志注入（CWE-117）防线测试。

应对 CodeQL py/log-injection（14 条，medium，如登录用户名、部门 id、技能 slug
等外部输入原样进日志，可被换行符伪造出额外日志行）。

修复策略是运行期全局 filter（src/utils/logger.py 的 NoLogInjectionFilter），
而非在每个调用点各写三个 .replace()。代价是 CodeQL 静态分析看不到运行期 filter
（同 net.py 的 is_ssrf_safe 处境），告警靠 codeql-config.yml 的 query-filters
排除——因此**本文件是防护回归的唯一保障**，不能依赖 code scanning 兜底。
"""
import logging

from src.utils.logger import NoLogInjectionFilter, install_no_log_injection_filter


def _record(msg, args=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )


def test_filter_escapes_crlf_in_msg():
    """消息里的 CR/LF 必须被转义成字面量，无法再伪造新日志行。"""
    f = NoLogInjectionFilter()
    r = _record("用户 admin\r\n登录成功")
    assert f.filter(r) is True
    assert "\n" not in r.msg and "\r" not in r.msg
    assert r.msg == "用户 admin\\r\\n登录成功"


def test_filter_escapes_crlf_in_tuple_args():
    """参数化日志（logger.info("%s", x)）的参数同样要净化。"""
    f = NoLogInjectionFilter()
    r = _record("更新技能 %s", ("sl\ru\ng",))
    assert f.filter(r) is True
    assert r.args == ("sl\\ru\\ng",)


def test_filter_escapes_crlf_in_dict_args():
    """logger.info("%(a)s", {"a": ...}) 形式同样覆盖。"""
    f = NoLogInjectionFilter()
    r = _record("%(a)s")
    # LogRecord 构造函数不接受 dict 型 args（内部会取 args[0] 而 dict 无此 key），
    # 真实链路是 logging 模块构造后再赋值，故此处同样构造后赋值。
    r.args = {"a": "x\ny"}
    assert f.filter(r) is True
    assert r.args == {"a": "x\\ny"}


def test_filter_leaves_clean_content_untouched():
    """无 CR/LF 的内容保持原样，不得误伤（含非字符串参数）。"""
    f = NoLogInjectionFilter()
    r = _record("正常消息 %s %s", ("abc", 123))
    assert f.filter(r) is True
    assert r.msg == "正常消息 %s %s"
    assert r.args == ("abc", 123)


def test_filter_never_suppresses_log_on_error():
    """净化逻辑自身出错时必须放行日志——绝不能因为防护而吞掉日志。"""
    f = NoLogInjectionFilter()
    r = _record("ok")
    r.args = object()  # 非 dict/可迭代，遍历会抛 TypeError
    assert f.filter(r) is True


def test_install_is_idempotent():
    """重复安装不应在 root/handler 上重复挂载 filter。"""
    root = logging.getLogger()
    install_no_log_injection_filter()
    install_no_log_injection_filter()
    n_root = sum(1 for x in root.filters if isinstance(x, NoLogInjectionFilter))
    assert n_root == 1, f"root 上应恰好 1 个 filter，实际 {n_root}"


def test_new_handler_gets_filter_after_rebuild():
    """setup_logger 会清空重建 handler，新建的 handler 必须被补齐防护。

    这条是本测试文件最关键的一条：早期实现用「root 上是否已装」的全局标记
    提前返回，一旦 setup_logger 二次执行，重建的 handler 就会永久失去防护。
    """
    root = logging.getLogger()
    install_no_log_injection_filter()
    h = logging.StreamHandler()
    root.addHandler(h)
    try:
        assert not any(isinstance(x, NoLogInjectionFilter) for x in h.filters)
        install_no_log_injection_filter()
        assert any(isinstance(x, NoLogInjectionFilter) for x in h.filters)
    finally:
        root.removeHandler(h)
