import asyncio
import signal

from app.config import get_settings
from app.logging.queue import LoggingQueue
from app.services.redis_service import RedisService
from app.services.request_log_queue_service import RequestLogQueueService
from app.services.token_usage_service import TokenUsageService


async def main() -> None:
    settings = get_settings()
    settings.validate_runtime_settings()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for item in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(item, stop_event.set)
        except NotImplementedError:
            pass

    redis_started = False
    logging_started = False
    request_log_started = False
    token_usage_started = False
    try:
        await RedisService.init()
        redis_started = True
        await LoggingQueue.start_background_workers()
        logging_started = True
        await RequestLogQueueService.start_background_workers()
        request_log_started = True
        await TokenUsageService.start_background_workers()
        token_usage_started = True
        await stop_event.wait()
    finally:
        if request_log_started:
            await RequestLogQueueService.stop_background_workers()
        if token_usage_started:
            await TokenUsageService.stop_background_workers()
        if logging_started:
            await LoggingQueue.stop_background_workers()
        if redis_started:
            await RedisService.aclose()


if __name__ == "__main__":
    asyncio.run(main())
