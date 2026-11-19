"""Общие фикстуры тестов плагина (без сети).

AA 5 автоматически оборачивает все URL, зарегистрированные через UrlHook,
декоратором main_character_required (allianceauth/urls.py → decorate_url_patterns).
Тестовые пользователи создаются через User.objects.create_user и не имеют
main character, поэтому все view-тесты падали бы с редиректом на /dashboard.
Здесь мы подменяем проверку на уровне тестового окружения — прод-поведение
плагина не меняется.
"""

import pytest


@pytest.fixture(autouse=True)
def _test_users_have_main_character(monkeypatch):
    """Тестовые пользователи считаются имеющими main character (без сети)."""
    from allianceauth.authentication import decorators as aa_decorators

    monkeypatch.setattr(aa_decorators, "user_has_main_character", lambda user: bool(user.is_authenticated))
