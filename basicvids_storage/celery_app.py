from celery import Celery

from basicvids_storage.settings import settings


celery_app = Celery(
    "basicvids_storage",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    task_default_queue=settings.VIDEO_TRANSCODE_QUEUE,
    imports=("basicvids_storage.tasks",),
    task_routes={
        "basicvids_storage.tasks.process_video": {"queue": settings.VIDEO_TRANSCODE_QUEUE},
    },
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)
