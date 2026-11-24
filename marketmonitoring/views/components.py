"""JSON-поиск типов по SDE для редактора фитов (§14, §5.10).

Страница «Components» и её CRUD убраны из интерфейса; остаётся только endpoint
поиска типов `type_search`, который использует редактор фитов (fit_form.html).
UI обращается только к БД плагина и справочникам SDE через eveuniverse (§5.10).
"""

import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from ..auth_hooks import has_edit

logger = logging.getLogger("marketmonitoring")


@login_required
def type_search(request):
    """JSON-поиск имён типов по SDE (edit, §14): EveType iloc name, лимит 20, один запрос (без N+1)."""
    if not has_edit(request.user):
        return JsonResponse({"results": [], "error": "permission denied"}, status=403)

    from eveuniverse.models import EveType

    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    rows = EveType.objects.filter(name__icontains=query).order_by("name").values("id", "name")[:20]
    return JsonResponse({"results": [{"type_id": row["id"], "name": row["name"]} for row in rows]})
