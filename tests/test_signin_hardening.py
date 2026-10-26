#!/usr/bin/env python3
"""vvvvrrrr 签到修复的场景验证。

在隔离副本中导入 workbuddy_daily（导入即会触发 auto_refresh，先用假响应屏蔽网络），
验证四类修复：
  A. run_account 遇非 JSON 响应不再崩溃（2026-10-23 起工作流连续 red 的根因）
  B. refresh_one 遇非 JSON 返回真实原因；get_json 失败降级不抛异常
  C. main() 单账号异常隔离，其余账号跑完，末尾非零退出码
  D. t_sign 只对真实成功/已签打 ✅，业务失败返回 "failed"
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import requests

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workbuddy_daily.py")
_HTML = object()   # 哨兵：响应体是非 JSON 的网关错误页


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = ("<html><body>502 Bad Gateway</body></html>"
                     if body is _HTML else json.dumps(body))

    def json(self):
        if self._body is _HTML:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class FakeSession:
    """按 URL 返回脚本化响应；记录调用。"""

    def __init__(self, behavior):
        self.headers = {}
        self.behavior = behavior
        self.calls = []

    def _resp(self, url, method):
        self.calls.append((method, url))
        return self.behavior(url, method)

    def get(self, url, **kw):
        return self._resp(url, "GET")

    def post(self, url, json=None, **kw):
        return self._resp(url, "POST")


def setup_module():
    """复制脚本到临时目录 + 假令牌库，屏蔽导入期的真实网络调用。"""
    tmp = tempfile.mkdtemp(prefix="wb-test-")
    shutil.copy(SRC, os.path.join(tmp, "workbuddy_daily.py"))
    with open(os.path.join(tmp, "wb_refresh_tokens.json"), "w", encoding="utf-8") as f:
        json.dump({"testuser": {"refresh_token": "dummy-rt", "access_token": "dummy.at"}}, f)
    sys.path.insert(0, tmp)
    real_post = requests.Session.post
    requests.Session.post = lambda self, *a, **k: FakeResp(401, {"code": 401, "msg": "unauthorized"})
    try:
        import workbuddy_daily as wd
    finally:
        requests.Session.post = real_post
    return wd


wd = setup_module()

# run_account 任务序列里与本次修复无关的重活，全部打桩成 no-op
NOOP_TASKS = ["t_team_3", "t_buddy_apps", "t_theme", "t_library", "t_canvas_automation",
              "t_expert_5", "t_template_5", "t_glm52", "t_black_cat", "t_lighthouse",
              "t_sequential_tasks", "t_sequential_tasks_2", "t_school_season", "t_badges",
              "t_lottery", "t_blindbox", "t_buddy_info", "t_travel", "t_redeem",
              "t_gift_compensation", "t_first_buddy", "t_makeup", "t_workstation",
              "t_unknown_tasks"]


def default_behavior(url, method):
    if url.endswith("/v2/billing/meter/daily-checkin"):
        return FakeResp(200, {"code": 0, "data": {"credit": 100, "streak_days": 26}})
    if url.endswith("/v2/activity/growth/tasks"):
        return FakeResp(200, {"code": 0, "data": {"tasks": [
            {"task_code": "done_1", "accept_status": "completed"}]}})
    return FakeResp(200, {"code": 0, "data": {}})


class RunAccountResilienceTests(unittest.TestCase):
    """A：非 JSON 响应不再炸掉整个工作流。

    注意 run_account 内部自建 session（new_api(tok)），必须 patch new_api
    把 FakeSession 接进去——直接传 session 参数是无效的。
    """

    def setUp(self):
        wd.NO_SCHOOL = True
        self.patches = [patch.object(wd, name, lambda *a, **k: None) for name in NOOP_TASKS]
        for p in self.patches:
            p.start()
        self.addCleanup(self._stop)

    def _stop(self):
        for p in self.patches:
            p.stop()

    def _run_account(self, behavior):
        s = FakeSession(behavior)
        with patch.object(wd, "new_api", lambda tok: s), patch("time.sleep"):
            return wd.run_account(1, {"note": "test", "access_token": "dummy.at"},
                                  do_desktop=False)

    def test_non_json_profile_does_not_crash(self):
        def behavior(url, method):
            if url.endswith("/v2/activity/growth/profile"):
                return FakeResp(502, _HTML)      # 复现 10-23 的崩溃现场
            return default_behavior(url, method)

        msgs, summary = self._run_account(behavior)
        self.assertEqual(summary["signed"], "claimed")
        self.assertEqual(summary["level"], "?")   # profile 挂了，等级降级为 ? 而不是崩溃

    def test_all_queries_non_json_still_completes(self):
        msgs, summary = self._run_account(lambda url, method: FakeResp(502, _HTML))
        self.assertEqual(summary["signed"], "failed")   # 签到接口也非 JSON → 如实失败
        self.assertIn("❌签到失败", "\n".join(msgs))

    def test_signin_runs_even_when_accept_all_cannot_load_tasks(self):
        # t_accept_all 在签到之前：它拿不到任务列表时也不能挡住签到
        def behavior(url, method):
            if url.endswith("/v2/activity/growth/tasks"):
                return FakeResp(502, _HTML)
            return default_behavior(url, method)

        msgs, summary = self._run_account(behavior)
        self.assertEqual(summary["signed"], "claimed")


class SigninResultTests(unittest.TestCase):
    """D：签到结果只对真实成功/已签打 ✅。"""

    def _run_sign(self, resp):
        s = FakeSession(lambda url, method: resp)
        lines = []
        wd.t_sign(s, "uid", "nick", lines.append)
        return "\n".join(lines)

    def test_success(self):
        s = FakeSession(lambda url, method: default_behavior(url, method))
        lines = []
        self.assertEqual(wd.t_sign(s, "uid", "nick", lines.append), "claimed")
        self.assertIn("✅签到成功 +100积分", "\n".join(lines))

    def test_already_signed(self):
        resp = FakeResp(400, {"code": 10001, "msg": "今天已签到，请明天再来"})
        lines = []
        self.assertEqual(wd.t_sign(FakeSession(lambda u, m: resp), "u", "n", lines.append), "already")
        self.assertIn("✅", "\n".join(lines))

    def test_enterprise_rejection_is_failure(self):
        # 与 88lin/workbuddy-auto-signin issue #17 同型：业务错误不能打 ✅
        resp = FakeResp(200, {"code": 10001, "msg": "企业账号不支持该操作"})
        lines = []
        self.assertEqual(wd.t_sign(FakeSession(lambda u, m: resp), "u", "n", lines.append), "failed")
        self.assertIn("❌签到失败", "\n".join(lines))
        self.assertIn("企业账号不支持该操作", "\n".join(lines))

    def test_non_json_is_failure(self):
        lines = []
        self.assertEqual(wd.t_sign(FakeSession(lambda u, m: FakeResp(502, _HTML)),
                                   "u", "n", lines.append), "failed")
        self.assertIn("❌签到失败", "\n".join(lines))

    def test_network_error_is_failure(self):
        def boom(url, **kw):
            raise requests.ConnectionError("connection reset")

        s = FakeSession(boom)
        lines = []
        self.assertEqual(wd.t_sign(s, "u", "n", lines.append), "failed")
        self.assertIn("❌签到请求失败", "\n".join(lines))


class RefreshTests(unittest.TestCase):
    """B：续期失败保留真实原因。"""

    def test_non_json_refresh_returns_reason(self):
        real_post = requests.Session.post
        requests.Session.post = lambda self, *a, **k: FakeResp(502, _HTML)
        try:
            with patch("time.sleep"):   # 502 会触发 5s/15s 退避，测试里不能真等
                at, err = wd.refresh_one("dummy-rt")
        finally:
            requests.Session.post = real_post
        self.assertIsNone(at)
        self.assertIn("非 JSON", err)

    def test_business_error_keeps_server_message(self):
        real_post = requests.Session.post
        requests.Session.post = lambda self, *a, **k: FakeResp(
            401, {"code": 40100, "msg": "refresh token 已过期"})
        try:
            at, err = wd.refresh_one("dummy-rt")
        finally:
            requests.Session.post = real_post
        self.assertIsNone(at)
        self.assertIn("refresh token 已过期", err)

    def test_get_json_degrades_instead_of_raising(self):
        def boom(url, **kw):
            raise requests.ConnectionError("no route")

        s = FakeSession(boom)
        with patch("time.sleep"):
            self.assertIsNone(wd.get_json(s, "https://x.invalid/a", retries=2))

    def test_get_json_retries_transient_then_succeeds(self):
        # 真实故障形态：2xx 但body 是网关错误页 → get_json 退避重试后成功
        calls = {"n": 0}

        def flaky(url, method, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                return FakeResp(200, _HTML)
            return FakeResp(200, {"code": 0, "data": {"ok": True}})

        s = FakeSession(flaky)
        with patch("time.sleep"):
            d = wd.get_json(s, "https://x.invalid/a", retries=3)
        self.assertEqual(d, {"code": 0, "data": {"ok": True}})
        self.assertEqual(calls["n"], 3)

    def test_get_json_returns_parsed_4xx_body(self):
        # 4xx 带 JSON 体要原样返回（调用方/interpret_failure 需要它），不重试
        s = FakeSession(lambda u, m: FakeResp(401, {"code": 40100, "msg": "x"}))
        with patch("time.sleep"):
            d = wd.get_json(s, "https://x.invalid/a", retries=3)
        self.assertEqual(d.get("code"), 40100)
        self.assertEqual(len(s.calls), 1)


class MainIsolationTests(unittest.TestCase):
    """C：单账号异常不再拖垮整个工作流。"""

    def _run_main(self, side_effects):
        accounts = [{"note": "a", "access_token": "t1"},
                    {"note": "b", "access_token": "t2"},
                    {"note": "c", "access_token": "t3"}]
        with patch.object(wd, "ACCOUNTS", accounts), \
                patch.object(wd, "QUERY_ONLY", False), \
                patch.object(wd, "SCHOOL_ONLY", False), \
                patch.object(wd, "NO_DESKTOP", True), \
                patch.object(wd, "NO_SCHOOL", True), \
                patch.object(wd, "send_notify_all"), \
                patch.object(wd, "run_account", side_effect=side_effects), \
                patch("time.sleep"):
            return wd.main()

    def test_all_success_returns_zero(self):
        def ok(idx, acc, desktop):
            return [], {"idx": idx, "note": acc["note"], "done": 1, "total": 1,
                        "rest": [], "level": "9", "energy": 5, "signed": "claimed"}

        self.assertEqual(self._run_main(ok), 0)

    def test_one_account_crash_does_not_stop_the_others(self):
        def boom(idx, acc, desktop):
            if idx == 2:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return [], {"idx": idx, "note": acc["note"], "done": 1, "total": 1,
                        "rest": [], "level": "9", "energy": 5, "signed": "claimed"}

        # 账号1/3 必须跑完；退出码为 1 让工作流如实标红（而不是静默假成功）
        with patch("builtins.print"):
            code = self._run_main(boom)
        self.assertEqual(code, 1)


class JsonOrEmptyTests(unittest.TestCase):
    """G：全量收口后的统一容错解析器。"""

    def test_non_json_returns_empty_dict(self):
        self.assertEqual(wd._json_or_empty(FakeResp(502, _HTML)), {})

    def test_non_dict_payload_returns_empty(self):
        # JSON 数组/null 不是业务 envelope，按空 dict 处理（调用点全是 .get 链）
        self.assertEqual(wd._json_or_empty(FakeResp(200, [1, 2])), {})
        self.assertEqual(wd._json_or_empty(FakeResp(200, None)), {})

    def test_dict_passthrough(self):
        self.assertEqual(wd._json_or_empty(FakeResp(200, {"code": 0})), {"code": 0})


class RefreshBackoffTests(unittest.TestCase):
    """E：续期瞬时错误退避重试（2026-10-24 三个账号同时 ReadTimeout 全灭的根因）。"""

    def _patch_post(self, side_effect):
        real_post = requests.Session.post
        requests.Session.post = side_effect
        self.addCleanup(setattr, requests.Session, "post", real_post)

    def test_network_error_retries_three_times_with_backoff(self):
        calls = {"n": 0}

        def boom(self, *a, **k):
            calls["n"] += 1
            raise requests.exceptions.ReadTimeout("read timeout")

        self._patch_post(boom)
        with patch("time.sleep") as slp:
            at, err = wd.refresh_one("dummy-rt", retries=3)
        self.assertIsNone(at)
        self.assertIn("ReadTimeout", err)
        self.assertEqual(calls["n"], 3)
        self.assertEqual([c.args[0] for c in slp.call_args_list], [5, 15])

    def test_401_is_not_retried(self):
        calls = {"n": 0}

        def unauth(self, *a, **k):
            calls["n"] += 1
            return FakeResp(401, {"code": 40100, "msg": "refresh token 已过期"})

        self._patch_post(unauth)
        with patch("time.sleep"):
            at, err = wd.refresh_one("dummy-rt", retries=3)
        self.assertIsNone(at)
        self.assertIn("refresh token 已过期", err)
        self.assertEqual(calls["n"], 1)   # 确定性失败重试无意义

    def test_5xx_non_json_retries_then_gives_up(self):
        calls = {"n": 0}

        def bad_gw(self, *a, **k):
            calls["n"] += 1
            return FakeResp(502, _HTML)

        self._patch_post(bad_gw)
        with patch("time.sleep"):
            at, err = wd.refresh_one("dummy-rt", retries=3)
        self.assertIsNone(at)
        self.assertIn("非 JSON", err)
        self.assertEqual(calls["n"], 3)

    def test_transient_then_success(self):
        calls = {"n": 0}

        def flaky(self, *a, **k):
            calls["n"] += 1
            if calls["n"] < 2:
                raise requests.exceptions.ConnectionError("conn reset by peer")
            return FakeResp(200, {"code": 0,
                                  "data": {"accessToken": "new-at", "refreshToken": "new-rt"}})

        self._patch_post(flaky)
        with patch("time.sleep"):
            at, nrt = wd.refresh_one("dummy-rt", retries=3)
        self.assertEqual(at, "new-at")
        self.assertEqual(nrt, "new-rt")
        self.assertEqual(calls["n"], 2)


class AccountPrecheckTests(unittest.TestCase):
    """F：账号级预检——AT 过期直接跳过，不拿死 token 逐个接口打 401。"""

    @staticmethod
    def _jwt(exp):
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        return "header.%s.signature" % payload

    def test_at_expired_detection(self):
        self.assertTrue(wd._at_expired(self._jwt(time.time() - 60)))
        self.assertFalse(wd._at_expired(self._jwt(time.time() + 86400)))

    def test_empty_token_counts_as_expired(self):
        self.assertTrue(wd._at_expired(""))

    def test_malformed_token_not_expired(self):
        # 解析不了就按未过期处理，把判定权交还给服务端 401
        self.assertFalse(wd._at_expired("not-a-jwt"))

    def test_filter_live_accounts_drops_dead(self):
        accs = [{"note": "活的", "access_token": self._jwt(time.time() + 86400)},
                {"note": "死的", "access_token": self._jwt(time.time() - 60)},
                {"note": "空的", "access_token": ""}]
        with patch("builtins.print"):
            live = wd.filter_live_accounts(accs)
        self.assertEqual([a["note"] for a in live], ["活的"])

    def test_all_alive_keeps_everything(self):
        accs = [{"note": "a", "access_token": self._jwt(time.time() + 86400)},
                {"note": "b", "access_token": self._jwt(time.time() + 3600)}]
        with patch("builtins.print"):
            self.assertEqual(len(wd.filter_live_accounts(accs)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
