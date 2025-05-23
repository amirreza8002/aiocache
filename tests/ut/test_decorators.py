import asyncio
import inspect
import random
import sys
from unittest.mock import ANY, create_autospec, patch

import pytest

from aiocache import cached, cached_stampede, multi_cached
from aiocache.backends.memory import SimpleMemoryCache
from aiocache.base import SENTINEL
from aiocache.decorators import _get_args_dict
from aiocache.lock import RedLock


async def stub(*args, value=None, seconds=0, **kwargs):
    await asyncio.sleep(seconds)
    if value:
        return str(value)
    return str(random.randint(1, 50))


class TestCached:
    @pytest.fixture
    def decorator(self, mock_cache):
        yield cached(cache=mock_cache)

    @pytest.fixture
    def decorator_call(self, decorator):
        d = decorator(stub)
        yield d

    @pytest.fixture(autouse=True)
    def spy_stub(self, mocker):
        module = sys.modules[globals()["__name__"]]
        mocker.spy(module, "stub")

    def test_init(self):
        cache = SimpleMemoryCache()
        c = cached(
            ttl=1,
            key_builder=lambda *args, **kw: "key",
            cache=cache,
            noself=False,
        )

        assert c.ttl == 1
        assert c.key_builder() == "key"
        assert c.cache is cache

    def test_get_cache_key_with_key(self, decorator):
        decorator.key_builder = lambda *args, **kw: "key"
        assert decorator.get_cache_key(stub, (1, 2), {"a": 1, "b": 2}) == "key"

    def test_get_cache_key_without_key_and_attr(self, decorator):
        assert (
            decorator.get_cache_key(stub, (1, 2), {"a": 1, "b": 2})
            == "stub(1, 2)[('a', 1), ('b', 2)]"
        )

    def test_get_cache_key_without_key_and_attr_noself(self, decorator):
        decorator.noself = True
        assert (
            decorator.get_cache_key(stub, ("self", 1, 2), {"a": 1, "b": 2})
            == "stub(1, 2)[('a', 1), ('b', 2)]"
        )

    def test_get_cache_key_with_key_builder(self, decorator):
        decorator.key_builder = lambda *args, **kwargs: kwargs["market"].upper()
        assert decorator.get_cache_key(stub, (), {"market": "es"}) == "ES"

    async def test_calls_get_and_returns(self, decorator, decorator_call):
        decorator.cache.get.return_value = 1

        await decorator_call()

        decorator.cache.get.assert_called_with("stub()[]")
        assert decorator.cache.set.call_count == 0
        assert stub.call_count == 0

    async def test_cache_read_disabled(self, decorator, decorator_call):
        await decorator_call(cache_read=False)

        assert decorator.cache.get.call_count == 0
        assert decorator.cache.set.call_count == 1
        assert stub.call_count == 1

    async def test_cache_write_disabled(self, decorator, decorator_call):
        decorator.cache.get.return_value = None

        await decorator_call(cache_write=False)

        assert decorator.cache.get.call_count == 1
        assert decorator.cache.set.call_count == 0
        assert stub.call_count == 1

    async def test_disable_params_not_propagated(self, decorator, decorator_call):
        decorator.cache.get.return_value = None

        await decorator_call(cache_read=False, cache_write=False)

        stub.assert_called_once_with()

    async def test_get_from_cache_returns(self, decorator, decorator_call):
        decorator.cache.get.return_value = 1
        assert await decorator.get_from_cache("key") == 1

    async def test_get_from_cache_exception(self, decorator, decorator_call):
        decorator.cache.get.side_effect = Exception
        assert await decorator.get_from_cache("key") is None

    async def test_get_from_cache_none(self, decorator, decorator_call):
        decorator.cache.get.return_value = None
        assert await decorator.get_from_cache("key") is None

    async def test_calls_fn_set_when_get_none(self, mocker, decorator, decorator_call):
        mocker.spy(decorator, "get_from_cache")
        mocker.spy(decorator, "set_in_cache")
        decorator.cache.get.return_value = None

        await decorator_call(value="value")

        assert decorator.get_from_cache.call_count == 1
        decorator.set_in_cache.assert_called_with("stub()[('value', 'value')]", "value")
        stub.assert_called_once_with(value="value")

    async def test_calls_fn_raises_exception(self, decorator, decorator_call):
        decorator.cache.get.return_value = None
        stub.side_effect = RuntimeError("foo")
        with pytest.raises(RuntimeError, match="foo"):
            assert await decorator_call()

    async def test_cache_write_waits_for_future(self, decorator, decorator_call):
        with patch.object(decorator, "get_from_cache", autospec=True, return_value=None) as m:
            await decorator_call()

            m.assert_awaited()

    async def test_cache_write_doesnt_wait_for_future(self, mocker, decorator, decorator_call):
        mocker.spy(decorator, "set_in_cache")
        with patch.object(decorator, "get_from_cache", autospec=True, return_value=None):
            await decorator_call(aiocache_wait_for_write=False, value="value")

        decorator.set_in_cache.assert_not_awaited()
        # decorator.set_in_cache.assert_called_once_with("stub()[('value', 'value')]", "value")

    async def test_set_calls_set(self, decorator, decorator_call):
        await decorator.set_in_cache("key", "value")
        decorator.cache.set.assert_called_with("key", "value", ttl=SENTINEL)

    async def test_set_calls_set_ttl(self, decorator, decorator_call):
        decorator.ttl = 10
        await decorator.set_in_cache("key", "value")
        decorator.cache.set.assert_called_with("key", "value", ttl=decorator.ttl)

    async def test_set_catches_exception(self, decorator, decorator_call):
        decorator.cache.set.side_effect = Exception
        assert await decorator.set_in_cache("key", "value") is None

    async def test_decorate(self, mock_cache):
        mock_cache.get.return_value = None

        @cached(cache=mock_cache)
        async def fn(n):
            return n

        assert await fn(1) == 1
        assert await fn(2) == 2
        assert fn.cache is mock_cache

    async def test_keeps_signature(self, mock_cache):
        @cached(cache=mock_cache)
        async def what(self, a, b):
            """Dummy function."""

        assert what.__name__ == "what"
        assert str(inspect.signature(what)) == "(self, a, b)"
        assert inspect.getfullargspec(what.__wrapped__).args == ["self", "a", "b"]

    async def test_reuses_cache_instance(self, mock_cache):
        @cached(cache=mock_cache)
        async def what():
            """Dummy function."""

        await what()
        await what()

        assert mock_cache.get.call_count == 2


class TestCachedStampede:
    @pytest.fixture
    def decorator(self, mock_cache):
        yield cached_stampede(cache=mock_cache)

    @pytest.fixture
    def decorator_call(self, decorator):
        yield decorator(stub)

    @pytest.fixture(autouse=True)
    def spy_stub(self, mocker):
        module = sys.modules[globals()["__name__"]]
        mocker.spy(module, "stub")

    def test_inheritance(self, mock_cache):
        assert isinstance(cached_stampede(mock_cache), cached)

    def test_init(self):
        cache = SimpleMemoryCache()
        c = cached_stampede(
            lease=3,
            ttl=1,
            key_builder=lambda *args, **kw: "key",
            cache=cache,
        )

        assert c.ttl == 1
        assert c.key_builder() == "key"
        assert c.cache is cache
        assert c.lease == 3

    async def test_calls_get_and_returns(self, decorator, decorator_call):
        decorator.cache.get.return_value = 1

        await decorator_call()

        decorator.cache.get.assert_called_with("stub()[]")
        assert decorator.cache.set.call_count == 0
        assert stub.call_count == 0

    async def test_calls_fn_raises_exception(self, decorator, decorator_call):
        decorator.cache.get.return_value = None
        stub.side_effect = RuntimeError("foo")
        with pytest.raises(RuntimeError, match="foo"):
            assert await decorator_call()

    async def test_calls_redlock(self, decorator, decorator_call):
        decorator.cache.get.return_value = None
        lock = create_autospec(RedLock, instance=True)

        with patch("aiocache.decorators.RedLock", autospec=True, return_value=lock):
            await decorator_call(value="value")

            assert decorator.cache.get.call_count == 2
            assert lock.__aenter__.call_count == 1
            assert lock.__aexit__.call_count == 1
            decorator.cache.set.assert_called_with(
                "stub()[('value', 'value')]", "value", ttl=SENTINEL
            )
            stub.assert_called_once_with(value="value")

    async def test_calls_locked_client(self, decorator, decorator_call):
        decorator.cache.get.side_effect = [None, None, None, "value"]
        decorator.cache._add.side_effect = [True, ValueError]
        lock1 = create_autospec(RedLock, instance=True)
        lock2 = create_autospec(RedLock, instance=True)

        with patch("aiocache.decorators.RedLock", autospec=True, side_effect=[lock1, lock2]):
            await asyncio.gather(decorator_call(value="value"), decorator_call(value="value"))

            assert decorator.cache.get.call_count == 4
            assert lock1.__aenter__.call_count == 1
            assert lock1.__aexit__.call_count == 1
            assert lock2.__aenter__.call_count == 1
            assert lock2.__aexit__.call_count == 1
            decorator.cache.set.assert_called_with(
                "stub()[('value', 'value')]", "value", ttl=SENTINEL
            )
            assert stub.call_count == 1


async def stub_dict(*args, keys=None, **kwargs):
    values = {"a": random.randint(1, 50), "b": random.randint(1, 50), "c": random.randint(1, 50)}
    return {k: values.get(k) for k in keys}


class TestMultiCached:
    @pytest.fixture
    def decorator(self, mock_cache):
        yield multi_cached(cache=mock_cache, keys_from_attr="keys")

    @pytest.fixture
    def decorator_call(self, decorator):
        d = decorator(stub_dict)
        decorator._conn = decorator.cache.get_connection()
        yield d

    @pytest.fixture(autouse=True)
    def spy_stub_dict(self, mocker):
        module = sys.modules[globals()["__name__"]]
        mocker.spy(module, "stub_dict")

    def test_init(self):
        cache = SimpleMemoryCache()
        mc = multi_cached(
            keys_from_attr="keys",
            key_builder=None,
            ttl=1,
            cache=cache,
        )

        def f():
            """Dummy function. Not called."""

        assert mc.ttl == 1
        assert mc.key_builder("key", f) == "key"
        assert mc.keys_from_attr == "keys"
        assert mc.cache is cache

    def test_get_cache_keys(self, decorator):
        keys = decorator.get_cache_keys(stub_dict, (), {"keys": ["a", "b"]})
        assert keys == (["a", "b"], ["a", "b"], [], -1)

    def test_get_cache_keys_empty_list(self, decorator):
        assert decorator.get_cache_keys(stub_dict, (), {"keys": []}) == ([], [], [], -1)

    def test_get_cache_keys_missing_kwarg(self, decorator):
        assert decorator.get_cache_keys(stub_dict, (), {}) == ([], [], [], -1)

    def test_get_cache_keys_arg_key_from_attr(self, decorator):
        def fake(keys, a=1, b=2):
            """Dummy function."""

        assert decorator.get_cache_keys(fake, (["a"],), {}) == (["a"], ["a"], [["a"]], 0)

    def test_get_cache_keys_with_none(self, decorator):
        assert decorator.get_cache_keys(stub_dict, (), {"keys": None}) == ([], [], [], -1)

    def test_get_cache_keys_with_key_builder(self, decorator):
        decorator.key_builder = lambda key, *args, **kwargs: kwargs["market"] + "_" + key.upper()
        assert decorator.get_cache_keys(stub_dict, (), {"keys": ["a", "b"], "market": "ES"}) == (
            ["a", "b"],
            ["ES_A", "ES_B"],
            [],
            -1,
        )

    async def test_get_from_cache(self, decorator, decorator_call):
        decorator.cache.multi_get.return_value = [1, 2, 3]

        assert await decorator.get_from_cache("a", "b", "c") == [1, 2, 3]
        decorator.cache.multi_get.assert_called_with(("a", "b", "c"))

    async def test_get_from_cache_no_keys(self, decorator, decorator_call):
        assert await decorator.get_from_cache() == []
        assert decorator.cache.multi_get.call_count == 0

    async def test_get_from_cache_exception(self, decorator, decorator_call):
        decorator.cache.multi_get.side_effect = Exception

        assert await decorator.get_from_cache("a", "b", "c") == [None, None, None]
        decorator.cache.multi_get.assert_called_with(("a", "b", "c"))

    async def test_get_from_cache_conn(self, decorator, decorator_call):
        decorator.cache.multi_get.return_value = [1, 2, 3]

        assert await decorator.get_from_cache("a", "b", "c") == [1, 2, 3]
        decorator.cache.multi_get.assert_called_with(("a", "b", "c"))

    async def test_calls_no_keys(self, decorator, decorator_call):
        await decorator_call(keys=[])
        assert decorator.cache.multi_get.call_count == 0
        assert stub_dict.call_count == 1

    async def test_returns_from_multi_set(self, mocker, decorator, decorator_call):
        mocker.spy(decorator, "get_from_cache")
        mocker.spy(decorator, "set_in_cache")
        decorator.cache.multi_get.return_value = [1, 2]

        assert await decorator_call(1, keys=["a", "b"]) == {"a": 1, "b": 2}
        decorator.get_from_cache.assert_called_once_with("a", "b")
        assert decorator.set_in_cache.call_count == 0
        assert stub_dict.call_count == 0

    async def test_calls_fn_multi_set_when_multi_get_none(self, mocker, decorator, decorator_call):
        mocker.spy(decorator, "get_from_cache")
        mocker.spy(decorator, "set_in_cache")
        decorator.cache.multi_get.return_value = [None, None]

        ret = await decorator_call(1, keys=["a", "b"], value="value")

        decorator.get_from_cache.assert_called_once_with("a", "b")
        decorator.set_in_cache.assert_called_with(ret, stub_dict, ANY, ANY)
        stub_dict.assert_called_once_with(1, keys=["a", "b"], value="value")

    async def test_cache_write_waits_for_future(self, mocker, decorator, decorator_call):
        mocker.spy(decorator, "set_in_cache")
        with patch.object(decorator, "get_from_cache", autospec=True, return_value=[None, None]):
            await decorator_call(1, keys=["a", "b"], value="value")

            decorator.set_in_cache.assert_awaited()

    async def test_cache_write_doesnt_wait_for_future(self, mocker, decorator, decorator_call):
        mocker.spy(decorator, "set_in_cache")
        with patch.object(decorator, "get_from_cache", autospec=True, return_value=[None, None]):
            with patch("aiocache.decorators.asyncio.ensure_future", autospec=True):
                await decorator_call(1, keys=["a", "b"], value="value",
                                     aiocache_wait_for_write=False)

        decorator.set_in_cache.assert_not_awaited()
        # decorator.set_in_cache.assert_called_once_with({"a": ANY, "b": ANY}, stub_dict, ANY, ANY)

    async def test_calls_fn_with_only_missing_keys(self, mocker, decorator, decorator_call):
        mocker.spy(decorator, "set_in_cache")
        decorator.cache.multi_get.return_value = [1, None]

        assert await decorator_call(1, keys=["a", "b"], value="value") == {"a": ANY, "b": ANY}

        decorator.set_in_cache.assert_called_once_with({"a": ANY, "b": ANY}, stub_dict, ANY, ANY)
        stub_dict.assert_called_once_with(1, keys=["b"], value="value")

    async def test_calls_fn_raises_exception(self, decorator, decorator_call):
        decorator.cache.multi_get.return_value = [None]
        stub_dict.side_effect = RuntimeError("foo")
        with pytest.raises(RuntimeError, match="foo"):
            assert await decorator_call(keys=[])

    async def test_cache_read_disabled(self, decorator, decorator_call):
        await decorator_call(1, keys=["a", "b"], cache_read=False)

        assert decorator.cache.multi_get.call_count == 0
        assert decorator.cache.multi_set.call_count == 1
        assert stub_dict.call_count == 1

    async def test_cache_write_disabled(self, decorator, decorator_call):
        decorator.cache.multi_get.return_value = [None, None]

        await decorator_call(1, keys=["a", "b"], cache_write=False)

        assert decorator.cache.multi_get.call_count == 1
        assert decorator.cache.multi_set.call_count == 0
        assert stub_dict.call_count == 1

    async def test_disable_params_not_propagated(self, decorator, decorator_call):
        decorator.cache.multi_get.return_value = [None, None]

        await decorator_call(1, keys=["a", "b"], cache_read=False, cache_write=False)

        stub_dict.assert_called_once_with(1, keys=["a", "b"])

    async def test_set_in_cache(self, decorator, decorator_call):
        await decorator.set_in_cache({"a": 1, "b": 2}, stub_dict, (), {})

        call_args = decorator.cache.multi_set.call_args[0][0]
        assert ("a", 1) in call_args
        assert ("b", 2) in call_args
        assert decorator.cache.multi_set.call_args[1]["ttl"] is SENTINEL

    async def test_set_in_cache_with_ttl(self, decorator, decorator_call):
        decorator.ttl = 10
        await decorator.set_in_cache({"a": 1, "b": 2}, stub_dict, (), {})

        assert decorator.cache.multi_set.call_args[1]["ttl"] == decorator.ttl

    async def test_set_in_cache_exception(self, decorator, decorator_call):
        decorator.cache.multi_set.side_effect = Exception

        assert await decorator.set_in_cache({"a": 1, "b": 2}, stub_dict, (), {}) is None

    async def test_decorate(self, mock_cache):
        mock_cache.multi_get.return_value = [None]

        @multi_cached(cache=mock_cache, keys_from_attr="keys")
        async def fn(keys=None):
            return {"test": 1}

        assert await fn(keys=["test"]) == {"test": 1}
        assert await fn(["test"]) == {"test": 1}
        assert fn.cache == mock_cache

    async def test_keeps_signature(self):
        @multi_cached(keys_from_attr="keys")
        async def what(self, keys=None, what=1):
            """Dummy function."""

        assert what.__name__ == "what"
        assert str(inspect.signature(what)) == "(self, keys=None, what=1)"
        assert inspect.getfullargspec(what.__wrapped__).args == ["self", "keys", "what"]

    async def test_key_builder(self):
        @multi_cached(cache=SimpleMemoryCache(), keys_from_attr="keys",
                      key_builder=lambda key, _, keys: key + 1)
        async def f(keys=None):
            return {k: k * 3 for k in keys}

        assert await f(keys=(1,)) == {1: 3}
        cached_value = await f.cache.get(2)
        assert cached_value == 3
        assert not await f.cache.exists(1)


def test_get_args_dict():
    def fn(a, b, *args, keys=None, **kwargs):
        """Dummy function."""

    args_dict = _get_args_dict(fn, ("a", "b", "c", "d"), {"what": "what"})
    assert args_dict == {"a": "a", "b": "b", "keys": None, "what": "what"}
