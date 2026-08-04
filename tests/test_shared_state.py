"""shared_state 模块测试。"""
import src.shared_state as ss


def test_app_instance_roundtrip():
    dummy = {"key": "value"}
    ss.set_app_instance(dummy)
    assert ss.get_app_instance() is dummy


def test_config_reload_callback_roundtrip():
    called = []
    def cb():
        called.append(1)

    ss.set_config_reload_callback(cb)
    cb2 = ss.get_config_reload_callback()
    cb2()
    assert len(called) == 1
