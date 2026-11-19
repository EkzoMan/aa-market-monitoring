"""Совместимость тестового окружения с Alliance Auth (без Redis).

Alliance Auth при старте инициализирует статистику задач через
``allianceauth.utils.cache.get_redis_client``, который требует django-redis
или redis-клиента в кэше. В тестовом окружении Redis отсутствует, поэтому
подменяем ``get_redis_client`` так, чтобы он возвращал объект-заглушку
с тем же интерфейсом, что и встроенный ``_RedisStub`` AA (атрибут
``IS_STUB`` и no-op методы). Патчить site-packages не нужно.

Подключается в settings через ``import testproject.test_env_compat``.
"""

_METHODS = ("delete", "incr", "zadd", "zcount", "zrangebyscore", "ping", "info")


class _RedisStub:
    """Заглушка redis-клиента, совместимая с AA EventSeries."""

    IS_STUB = True

    def info(self, *args, **kwargs):
        return {"redis_version": "0.0.0", "valkey_version": "0.0.0"}

    def incr(self, *args, **kwargs):
        return 0

    def __getattr__(self, name):
        if name in _METHODS:

            def _noop(*args, **kwargs):
                return None

            return _noop
        raise AttributeError(name)


import allianceauth.utils.cache as _aa_cache  # noqa: E402

_aa_cache.get_redis_client = lambda: _RedisStub()
