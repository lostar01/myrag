from celery import Celery
from settings import settings

app = Celery(
    "rag_tasks",
    broker=settings.celery.broker_url,
    backend=settings.celery.backend_url
)

# 任务自动发现
# app.autodiscover_tasks(["tasks"])

# 全局配置
app.conf.update(
    task_acks_late = True,
    task_reject_on_worker_lost = True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=False,
    task_time_limit=1200,      # 单任务最长20分钟，大PDF防卡死
    task_soft_time_limit=1100,
    task_retry_limit=2,        # 失败重试2次
    task_default_retry_delay=5
)

import tasks