"""通用 Identity Framework 单测（backend/adapters/identity.py）。

覆盖：模型合并语义、缓存 TTL/落盘、resolver 模板流程、Fail Soft 硬约束、
三态校验、凭证三级降级。

说明：这里注入 fake fetch 是为了**确定性地测决策分支**（哪条策略先跑、
校验失败怎么处理），不是用 mock 冒充线上验证 —— 线上真实请求的验证在
docs/kuaishou_identity_report.md 的实验记录里。
"""

import json
import os

import pytest

from backend.adapters.identity import (
    CredentialLadder,
    CredentialLevel,
    HttpResponse,
    IdentityCache,
    IdentityKind,
    IdentityQuery,
    IdentityResolver,
    IdentitySource,
    PrincipalIdentity,
    ResolveContext,
    ResolveStrategy,
    VerifyOutcome,
)


# ---------------------------------------------------------------- 模型
def test_merge_只填空位不覆盖已有值():
    """先验优先：早跑的策略结论不能被后跑的推翻。"""
    a = PrincipalIdentity(platform="ks", principal_id="P1", nickname="")
    b = PrincipalIdentity(platform="ks", principal_id="P2", nickname="小明")
    a.merge(b)
    assert a.principal_id == "P1"   # 已有值不被覆盖
    assert a.nickname == "小明"      # 空位被补上


def test_merge_取较大置信度并累加trace():
    a = PrincipalIdentity(confidence=0.5, trace=["s1"])
    b = PrincipalIdentity(confidence=0.9, trace=["s2", "s1"])
    a.merge(b)
    assert a.confidence == 0.9
    assert a.trace == ["s1", "s2"]  # 去重累加


def test_merge_none_安全():
    a = PrincipalIdentity(principal_id="P1")
    assert a.merge(None).principal_id == "P1"


def test_from_dict_脏数据返回None():
    assert PrincipalIdentity.from_dict(None) is None
    assert PrincipalIdentity.from_dict("not a dict") is None
    assert PrincipalIdentity.from_dict({"unknown_key": 1}) is None


def test_from_dict_忽略未知字段():
    ident = PrincipalIdentity.from_dict({"principal_id": "P1", "future_field": 42})
    assert ident is not None and ident.principal_id == "P1"


def test_条目自带ttl优先于全局ttl():
    ident = PrincipalIdentity(principal_id="P1", resolved_at=1000.0, ttl=10)
    assert ident.is_expired(ttl=99999, now=1005.0) is False
    assert ident.is_expired(ttl=99999, now=1020.0) is True  # 自带 ttl 说了算


# ---------------------------------------------------------------- 缓存
def test_缓存_未解析出主键的不入库():
    cache = IdentityCache()
    cache.put("k", PrincipalIdentity(nickname="只有昵称"))
    assert cache.get("k") is None


def test_缓存_自动补时间戳避免立即过期():
    cache = IdentityCache()
    cache.put("k", PrincipalIdentity(platform="ks", principal_id="P1"))
    assert cache.get("k") is not None


def test_缓存_过期未命中():
    cache = IdentityCache(ttl=10)
    ident = PrincipalIdentity(platform="ks", principal_id="P1")
    cache.put("k", ident)
    assert cache.get("k", now=ident.resolved_at + 999) is None


def test_缓存_落盘与重载(tmp_path):
    path = str(tmp_path / "id.json")
    c1 = IdentityCache(path=path)
    c1.put("ks:x", PrincipalIdentity(platform="ks", principal_id="P1"))
    assert c1.save() is True
    c2 = IdentityCache(path=path)
    assert c2.get("ks:x").principal_id == "P1"
    # principal_id 二级索引，换个入口也能查到
    assert c2.get("ks:P1") is not None


def test_缓存_坏文件不炸(tmp_path):
    path = str(tmp_path / "bad.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    cache = IdentityCache(path=path)
    assert cache.get("anything") is None  # 退化为空缓存，不抛异常


def test_缓存_无path时save返回False():
    assert IdentityCache().save() is False


# ---------------------------------------------------------------- Resolver
class _FakeStrategy(ResolveStrategy):
    name = "fake"
    source = IdentitySource.API
    cost = 1
    confidence = 0.8

    def __init__(self, pid="", boom=False):
        self.pid = pid
        self.boom = boom
        self.calls = 0

    def run(self, q, ctx):
        self.calls += 1
        if self.boom:
            raise RuntimeError("策略内部炸了")
        return self.build(principal_id=self.pid) if self.pid else None


class _Resolver(IdentityResolver):
    platform = "test"

    def __init__(self, sts, **kw):
        super().__init__(cache=IdentityCache(), **kw)
        self._sts = sts
        self.verify_outcome = VerifyOutcome.PASS

    def strategies(self):
        return self._sts

    def verify(self, ident, ctx):
        return self.verify_outcome


def test_策略抛异常不影响后续策略():
    """Fail Soft 硬约束：一条策略炸掉不能拖垮整轮。"""
    boom, good = _FakeStrategy(boom=True), _FakeStrategy(pid="P1")
    ident = _Resolver([boom, good]).resolve("x")
    assert ident is not None and ident.principal_id == "P1"
    assert boom.calls == 1 and good.calls == 1


def test_解出主键后短路不再跑后续策略():
    first, second = _FakeStrategy(pid="P1"), _FakeStrategy(pid="P2")
    r = _Resolver([first, second])
    assert r.resolve("x").principal_id == "P1"
    assert second.calls == 0  # 省掉一次无谓的网络请求


def test_按cost升序执行():
    cheap, pricey = _FakeStrategy(pid="CHEAP"), _FakeStrategy(pid="PRICEY")
    cheap.cost, pricey.cost = 0, 5
    assert _Resolver([pricey, cheap]).resolve("x").principal_id == "CHEAP"


def test_全部策略失败返回None():
    assert _Resolver([_FakeStrategy(), _FakeStrategy()]).resolve("x") is None


def test_空输入不触发任何策略():
    st = _FakeStrategy(pid="P1")
    assert _Resolver([st]).resolve("") is None
    assert st.calls == 0


def test_config显式配置优先级最高且跳过所有策略():
    st = _FakeStrategy(pid="FROM_NET")
    ident = _Resolver([st]).resolve("x", hints={"principal_id": "FROM_CONFIG"})
    assert ident.principal_id == "FROM_CONFIG"
    assert ident.identity_source == IdentitySource.CONFIG
    assert st.calls == 0


def test_命中缓存不再跑策略():
    st = _FakeStrategy(pid="P1")
    r = _Resolver([st])
    r.resolve("x")
    r.resolve("x")
    assert st.calls == 1


def test_force跳过缓存():
    st = _FakeStrategy(pid="P1")
    r = _Resolver([st])
    r.resolve("x")
    r.resolve("x", force=True)
    assert st.calls == 2


def test_校验FAIL丢弃结果():
    r = _Resolver([_FakeStrategy(pid="P1")])
    r.verify_outcome = VerifyOutcome.FAIL
    assert r.resolve("x") is None


def test_校验UNKNOWN放行但短ttl且标记未校验():
    """校验没做成 ≠ 校验通过 —— 不能让「配错人」靠网络抖动蒙混过关。"""
    r = _Resolver([_FakeStrategy(pid="P1")])
    r.verify_outcome = VerifyOutcome.UNKNOWN
    ident = r.resolve("x")
    assert ident is not None
    assert ident.extra["verified"] is False
    assert ident.ttl == r.unverified_ttl  # 下一轮会重验


def test_校验PASS标记已校验且用默认ttl():
    ident = _Resolver([_FakeStrategy(pid="P1")]).resolve("x")
    assert ident.extra["verified"] is True and ident.ttl == 0.0


def test_校验器自身抛异常记为UNKNOWN():
    class _Boom(_Resolver):
        def verify(self, ident, ctx):
            raise RuntimeError("校验器炸了")

    ident = _Boom([_FakeStrategy(pid="P1")]).resolve("x")
    assert ident is not None and ident.extra["verified"] is False


def test_verify返回bool向后兼容():
    class _BoolVerify(_Resolver):
        def verify(self, ident, ctx):
            return False

    assert _BoolVerify([_FakeStrategy(pid="P1")]).resolve("x") is None


def test_strategies本身抛异常不炸():
    class _Broken(IdentityResolver):
        platform = "test"

        def strategies(self):
            raise RuntimeError("构建策略列表炸了")

    assert _Broken(cache=IdentityCache()).resolve("x") is None


def test_resolve_detailed_保留partial供调用方使用():
    """解不出主键时，沿途拿到的昵称等信息也别浪费。"""
    class _PartialOnly(ResolveStrategy):
        name = "partial"
        cost = 1

        def run(self, q, ctx):
            return self.build(nickname="小明")

    res = _Resolver([_PartialOnly()]).resolve_detailed("x")
    assert res.ok is False
    assert res.partial.nickname == "小明"


def test_http_log记录请求过程():
    class _Net(ResolveStrategy):
        name = "net"
        cost = 1

        def run(self, q, ctx):
            ctx.get("https://example.com/a")
            return self.build(principal_id="P1")

    r = _Resolver([_Net()])
    r.fetch = lambda url, headers=None, timeout=10: HttpResponse(url=url, status=200, text="hi")
    res = r.resolve_detailed("x")
    assert res.http_log[0]["url"] == "https://example.com/a"
    assert res.http_log[0]["status"] == 200


def test_classify异常时退化为UNKNOWN():
    class _BadClassify(_Resolver):
        def classify(self, raw):
            raise RuntimeError("分类炸了")

    assert _BadClassify([_FakeStrategy(pid="P1")]).resolve("x") is not None


# ---------------------------------------------------------------- HTTP
def test_http_fetch_网络异常不抛():
    from backend.adapters.identity import http_fetch

    resp = http_fetch("http://127.0.0.1:9/never", timeout=1)
    assert resp.ok is False and resp.status == 0 and resp.error


# ---------------------------------------------------------------- 凭证阶梯
def test_阶梯_缺凭证的等级自动跳过():
    assert [lv for lv, _ in CredentialLadder().levels()] == [CredentialLevel.ANONYMOUS]
    got = [lv for lv, _ in CredentialLadder(did="d", cookie="c").levels()]
    assert got == [CredentialLevel.ANONYMOUS, CredentialLevel.DEVICE, CredentialLevel.COOKIE]


def test_阶梯_匿名成功就不上更高等级():
    ladder = CredentialLadder(did="d", cookie="c")
    r = ladder.run(lambda h, lv: "OK")
    assert r.ok and r.level_used == CredentialLevel.ANONYMOUS
    assert r.levels_tried == [CredentialLevel.ANONYMOUS]


def test_阶梯_逐级降级直到成功():
    ladder = CredentialLadder(did="d", cookie="c")
    r = ladder.run(lambda h, lv: "OK" if lv == CredentialLevel.COOKIE else None)
    assert r.ok and r.level_used == CredentialLevel.COOKIE
    assert r.levels_tried == [CredentialLevel.ANONYMOUS, CredentialLevel.DEVICE,
                              CredentialLevel.COOKIE]


def test_阶梯_异常视为该级失败继续降级():
    def fn(headers, level):
        if level != CredentialLevel.DEVICE:
            raise RuntimeError("挂了")
        return "OK"

    r = CredentialLadder(did="d", cookie="c").run(fn)
    assert r.ok and r.level_used == CredentialLevel.DEVICE


def test_阶梯_should_retry判定风控结果为失败():
    """快手风控返回的是 200 + result=400002，得靠内容判失败。"""
    calls = []

    def fn(headers, level):
        calls.append(level)
        return {"result": 400002} if level != CredentialLevel.COOKIE else {"result": 1}

    r = CredentialLadder(did="d", cookie="c").run(
        fn, should_retry=lambda v: v.get("result") == 400002)
    assert r.ok and r.level_used == CredentialLevel.COOKIE


def test_阶梯_全失败不抛异常():
    r = CredentialLadder(did="d").run(lambda h, lv: None)
    assert r.ok is False and r.level_used == "" and len(r.attempts) == 2


def test_阶梯_did等级带client_key():
    ladder = CredentialLadder(did="D1", client_key="CK")
    hdr = dict(ladder.levels())[CredentialLevel.DEVICE]
    assert "did=D1" in hdr["Cookie"] and "client_key=CK" in hdr["Cookie"]
