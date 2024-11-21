import uuid
from typing import Any, Dict, AsyncGenerator, Callable, Awaitable, Optional
import asyncio
import time
from contextlib import asynccontextmanager
from auralis.common.definitions.scheduler import QueuedRequest, TaskState
from auralis.common.logging.logger import setup_logger


class TwoPhaseScheduler:
    def __init__(
            self,
            second_phase_concurrency: int = 10,
            request_timeout: float = None,
            generator_timeout: float = None
    ):
        # Core configuration
        self.second_phase_concurrency = second_phase_concurrency
        self.request_timeout = request_timeout
        self.generator_timeout = generator_timeout
        self.logger = setup_logger(__file__)

        # State management
        self.is_running = False
        self.request_queue = None
        self.active_requests = {}
        self.queue_processor_tasks = []

        # Concurrency controls
        self.second_phase_sem = None
        self.active_generator_count = 0
        self.generator_count_lock = asyncio.Lock()
        self.cleanup_lock = asyncio.Lock()

    async def start(self):
        if self.is_running:
            return

        self.request_queue = asyncio.Queue()
        self.second_phase_sem = asyncio.Semaphore(self.second_phase_concurrency)
        self.is_running = True
        self.queue_processor_tasks = [
            asyncio.create_task(self._process_queue())
            for _ in range(self.second_phase_concurrency)
        ]

    async def _process_queue(self):
        """Continuously process requests from the queue."""
        while self.is_running:
            try:
                request = await self.request_queue.get()
                if request.state == TaskState.QUEUED:
                    async with self._request_lifecycle(request.id):
                        self.active_requests[request.id] = request
                        await self._process_request(request)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Queue processing error: {e}")
                await asyncio.sleep(1)

    @asynccontextmanager
    async def _request_lifecycle(self, request_id: str):
        try:
            yield
        finally:
            async with self.cleanup_lock:
                self.active_requests.pop(request_id, None)

    async def _process_request(self, request: QueuedRequest):
        """Handle the two-phase processing of a request."""
        try:
            self.logger.info(f"Starting request {request.id}")
            # Phase 1: Initial processing
            await self._handle_first_phase(request)

            # Phase 2: Parallel processing
            await self._handle_second_phase(request)

            if not request.error:
                request.state = TaskState.COMPLETED

        except Exception as e:
            request.error = e
            request.state = TaskState.FAILED
            self.logger.error(f"Request {request.id} failed: {e}")
        finally:
            request.completion_event.set()

    async def _handle_first_phase(self, request: QueuedRequest):
        """Execute the first phase of processing."""
        request.state = TaskState.PROCESSING_FIRST
        try:
            request.first_phase_result = await asyncio.wait_for(
                request.first_fn(request.input),
                timeout=self.request_timeout
            )
            request.generators_count = len(request.first_phase_result.get('parallel_inputs', []))
            request.state = TaskState.PROCESSING_SECOND
        except asyncio.TimeoutError:
            raise TimeoutError(f"First phase timeout after {self.request_timeout}s")

    async def _handle_second_phase(self, request: QueuedRequest):
        """Execute the second phase of processing."""
        parallel_inputs = request.first_phase_result.get('parallel_inputs', [])
        generator_tasks = [
            asyncio.create_task(self._process_generator(request, gen_input, idx))
            for idx, gen_input in enumerate(parallel_inputs)
        ]

        try:
            await asyncio.wait_for(
                asyncio.gather(*generator_tasks, return_exceptions=True),
                timeout=self.request_timeout
            )
        except asyncio.TimeoutError:
            for task in generator_tasks:
                if not task.done():
                    task.cancel()
            raise TimeoutError(f"Second phase timeout after {self.request_timeout}s")

    async def _process_generator(
            self,
            request: QueuedRequest,
            generator_input: Any,
            sequence_idx: int,
    ):
        async with self.second_phase_sem:
            try:
                await self._init_generator(request, sequence_idx)
                await self._run_generator(request, generator_input, sequence_idx)
            except asyncio.CancelledError:
                self.logger.warning(f"Generator {sequence_idx} cancelled for request {request.id}")
                raise
            except Exception as e:
                self._handle_generator_error(request, sequence_idx, e)
            finally:
                await self._cleanup_generator(request, sequence_idx)

    async def _init_generator(self, request: QueuedRequest, sequence_idx: int):
        async with self.generator_count_lock:
            self.active_generator_count += 1
            if not hasattr(request, 'generator_events'):
                request.generator_events = {}
            request.generator_events[sequence_idx] = asyncio.Event()

    async def _run_generator(self, request: QueuedRequest, generator_input: Any, sequence_idx: int):
        generator = request.second_fn(generator_input)
        buffer = request.sequence_buffers[sequence_idx]

        while True:
            try:
                item = await asyncio.wait_for(
                    generator.__anext__(),
                    timeout=self.generator_timeout
                )
                event = asyncio.Event()
                event.set()
                buffer.append((item, event))
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                raise TimeoutError(f"Generator {sequence_idx} timed out")

    def _handle_generator_error(self, request: QueuedRequest, sequence_idx: int, error: Exception):
        self.logger.error(f"Generator {sequence_idx} failed for request {request.id}: {error}")
        if request.error is None:
            request.error = error

    async def _cleanup_generator(self, request: QueuedRequest, sequence_idx: int):
        async with self.generator_count_lock:
            self.active_generator_count -= 1
            request.completed_generators += 1
            if sequence_idx in request.generator_events:
                request.generator_events[sequence_idx].set()

    async def _yield_ordered_outputs(self, request: QueuedRequest) -> AsyncGenerator[Any, None]:
        """Yield outputs in sequence order."""
        current_index = 0
        last_progress = time.time()

        while not self._is_processing_complete(request):
            if self._check_timeout(last_progress):
                raise TimeoutError("No progress in output generation")

            if request.error:
                raise request.error

            if current_index in request.sequence_buffers:
                buffer = request.sequence_buffers[current_index]
                if buffer:
                    item, event = buffer[0]
                    try:
                        await asyncio.wait_for(event.wait(), timeout=self.generator_timeout)
                        yield item
                        buffer.pop(0)
                        last_progress = time.time()
                    except asyncio.TimeoutError:
                        raise TimeoutError(f"Timeout waiting for item in sequence {current_index}")

                    if self._can_advance_sequence(request, current_index):
                        current_index += 1

            await asyncio.sleep(0.01)

    def _is_processing_complete(self, request: QueuedRequest) -> bool:
        return (request.state in (TaskState.COMPLETED, TaskState.FAILED) and
                request.completed_generators >= request.generators_count and
                all(len(buffer) == 0 for buffer in request.sequence_buffers.values()))

    def _check_timeout(self, last_progress: float) -> bool:
        return self.request_timeout and time.time() - last_progress > self.request_timeout

    def _can_advance_sequence(self, request: QueuedRequest, current_index: int) -> bool:
        return (hasattr(request, 'generator_events') and
                current_index in request.generator_events and
                request.generator_events[current_index].is_set())

    async def run(
            self,
            inputs: Any,
            first_phase_fn: Callable[[Any], Awaitable[Any]],
            second_phase_fn: Callable[[Dict], AsyncGenerator],
            request_id: str = None,
    ) -> AsyncGenerator[Any, None]:
        if not self.is_running:
            await self.start()

        request = QueuedRequest(
            id=request_id,
            input=inputs,
            first_fn=first_phase_fn,
            second_fn=second_phase_fn
        )

        await self.request_queue.put(request)

        try:
            async for item in self._yield_ordered_outputs(request):
                yield item

            await asyncio.wait_for(
                request.completion_event.wait(),
                timeout=self.request_timeout
            )
            if request.error:
                raise request.error

        finally:
            async with self.cleanup_lock:
                self.active_requests.pop(request.id, None)

    async def shutdown(self):
        self.is_running = False

        for task in self.queue_processor_tasks:
            if task and not task.done():
                task.cancel()

        await asyncio.gather(*self.queue_processor_tasks, return_exceptions=True)

        if self.active_requests:
            await asyncio.gather(
                *(request.completion_event.wait() for request in self.active_requests.values()),
                return_exceptions=True
            )