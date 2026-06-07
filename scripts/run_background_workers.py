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

    await RedisService.init()
    await LoggingQueue.start_background_workers()
    await RequestLogQueueService.start_background_workers()
    await TokenUsageService.start_background_workers()
    try:
        await stop_event.wait()
    finally:
        await RequestLogQueueService.stop_background_workers()
        await TokenUsageService.stop_background_workers()
        await LoggingQueue.stop_background_workers()
        await RedisService.aclose()


if __name__ == "__main__":
    asyncio.run(main())
