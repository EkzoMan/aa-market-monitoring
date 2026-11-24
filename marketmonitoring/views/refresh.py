"""Обновление данных (§12.4, §14): ручной запуск цепочки синков и JSON-статус для поллинга.

UI не выполняет ESI-запросы сам — только ставит задачи Celery (§5.10).
"""

import logging
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Max
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from ..auth_hooks import has_edit, has_view
from ..constants import SyncRunStatus, TriggerKind
from ..esi.selectors import is_jita_enabled
from ..models import Fit, SyncRun
from ..tasks import (
    SYNC_LOCK_KEY,
    sync_contracts,
    sync_jita_book,
    sync_region_orders,
    sync_structure_orders,
)

logger = logging.getLogger("marketmonitoring")

#: Таймаут блокировки ручного обновления (§12.4).
SYNC_LOCK_TIMEOUT = 3600


@login_required
@require_POST
def refresh(request):
    """«Обновить сейчас» (edit, §14): блокировка mm:sync_lock + цепочка sync_* с trigger=manual.

    Запускаются заказы регионов (обновляют статус NPC-станций и локаций-систем),
    заказы структур и открытые контракты; стакан Jita — только если локация
    Jita включена в Locations (иначе задача не ставится, ESI не вызывается).
    Итого 4 задачи при включённой Jita и 3 при выключенной.
    При занятой блокировке — 409 «обновление уже выполняется» (§12.4).
    Значение блокировки — уникальный токен запуска: он передаётся задачам цепочки,
    и финальная задача (recalc_stock_levels) снимает блокировку только при
    совпадении токена — параллельный запуск не снимается чужой цепочкой (§12.4).
    Уведомления о дефиците при manual не порождаются (§12.4 — цепочка задач
    сама пропускает notify_deficit при trigger=manual).
    """
    if not has_edit(request.user):
        messages.warning(request, _("Permission denied."))
        return redirect("marketmonitoring:index")

    lock_token = uuid.uuid4().hex
    if not cache.add(SYNC_LOCK_KEY, lock_token, timeout=SYNC_LOCK_TIMEOUT):
        logger.info("Manual refresh rejected: sync lock is held")
        return HttpResponse(_("Update is already running."), status=409)

    if is_jita_enabled():
        sync_jita_book.delay(trigger=TriggerKind.MANUAL, lock_token=lock_token)
    sync_region_orders.delay(trigger=TriggerKind.MANUAL, lock_token=lock_token)
    sync_structure_orders.delay(trigger=TriggerKind.MANUAL, lock_token=lock_token)
    sync_contracts.delay(trigger=TriggerKind.MANUAL, lock_token=lock_token)
    messages.success(request, _("Update started."))
    return redirect("marketmonitoring:index")


@login_required
def refresh_status(request):
    """JSON-статус для клиентского поллинга dashboard (§14): in_progress / last_checked_at / last_result."""
    if not has_view(request.user):
        return JsonResponse({"error": "permission denied"}, status=403)

    raw = cache.get(SYNC_LOCK_KEY)
    in_progress = False
    lock_age = None
    if raw is not None:
        # Значение блокировки — токен запуска; возраст вычислим только для
        # унаследованных значений-таймстампов (совместимость и тесты).
        try:
            lock_age = max(0.0, timezone.now().timestamp() - float(raw))
        except (TypeError, ValueError):
            lock_age = None
        # Блокировка «протухла» только по таймауту кэша; возраст — для клиента.
        in_progress = True

    last_checked_at = Fit.objects.aggregate(Max("last_checked_at"))["last_checked_at__max"]
    last_run = SyncRun.objects.order_by("-started_at").first()
    return JsonResponse(
        {
            "in_progress": in_progress,
            "lock_age_seconds": lock_age,
            "last_checked_at": last_checked_at.isoformat() if last_checked_at else None,
            "last_result": (
                {
                    "task_name": last_run.task_name,
                    "trigger": last_run.trigger,
                    "status": last_run.status,
                    "started_at": last_run.started_at.isoformat(),
                    "finished_at": last_run.finished_at.isoformat() if last_run.finished_at else None,
                    "status_ok": last_run.status == SyncRunStatus.OK,
                }
                if last_run
                else None
            ),
        }
    )
