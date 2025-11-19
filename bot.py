# bot.py
import os
import re
import asyncio
from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.exotel import ExotelFrameSerializer
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.groq.stt import GroqSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from call_memory import pop_next_outbound_call  # ✅ NEW

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

logger.add(
    os.path.join(LOG_DIR, "bot.log"),
    rotation="10 MB",
    retention="7 days",
    compression="zip",
    enqueue=True,
    backtrace=True,
    diagnose=True,
    level="DEBUG",
)

load_dotenv(override=True)

GOODBYE_RE = re.compile(
    r"\b(good\s*bye|goodbye|bye|thanks\,?\s*bye|thank you\.?\s*goodbye|bas,\s*theek\s*hai|band\s*karo)\b",
    re.IGNORECASE,
)
NOT_INTERESTED_RE = re.compile(
    r"(not\s*interested|don.?t\s*call|stop\s*calling|nahi\s*chahiye|mujhe\s*nahi\s*chahiye|vaddu|malli\s*call\s*cheyyakandi)",
    re.IGNORECASE,
)


async def run_bot(transport: BaseTransport, handle_sigint: bool, customer_name: str = ""):
    from config import messages

    tools = [
        {
            "type": "function",
            "function": {
                "name": "end_call",
                "description": (
                    "CRITICAL: End the phone call immediately. You MUST use this function when:\n"
                    "1. You say goodbye to the customer\n"
                    "2. Customer says goodbye or not interested\n"
                    "3. Conversation is complete (all details collected and confirmed)\n"
                    "4. Wrong person answered\n"
                    "NEVER say goodbye without calling this function."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": (
                                "Reason for ending the call "
                                "(e.g., 'customer_not_interested', "
                                "'conversation_complete', 'customer_goodbye', 'wrong_person')"
                            ),
                        }
                    },
                    "required": ["reason"],
                },
            },
        }
    ]

    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o",
        tools=tools,
        tool_choice="auto",
    )

    stt = OpenAISTTService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o-transcribe",
        prompt="Expect multilingual indian accent and indian languages.",
    )

    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY"),
        model="bulbul:v2",
        voice_id="manisha",
        sample_rate=8000,
    )

    context = LLMContext(messages)

    # ✅ If we already know the name, tell the LLM
    if customer_name:
        context.messages.insert(
            1,
            {
                "role": "system",
                "content": f"""
    You already know the customer's name is "{customer_name}" from the dialer.

    SPECIAL RULES WHEN NAME IS KNOWN:

    - You STILL MUST wait for the customer to say something first (e.g. "Hello?", "Yes", "Haan", "Bolo", etc.).
    - On your VERY FIRST reply after the customer speaks, you MUST:
    1) Greet them, and
    2) Explicitly CONFIRM the name with EXACTLY ONE short question, for example:

    English:
    - "Hello, am I speaking with {customer_name}?"

    Hindi:
    - "Hello, kya main {customer_name} se baat kar rahi hoon?"

    Telugu:
    - "Hello, nenu {customer_name} garitho maatladutunnaana?"

    - Do NOT ask "What is your name?" or "May I know your name?" when you already know {customer_name}.
    - Treat this as STAGE 1 - NAME VERIFICATION:
    - If they clearly confirm ("Yes", "Haan", "Speaking", "This is {customer_name}", etc.):
        → Briefly acknowledge (e.g. "Nice to speak with you, {customer_name}.")
        → IMMEDIATELY move to the INTEREST CHECK (Stage 2 in the main instructions):
            e.g. "Would you be interested in hearing about our loan options?"
    - If they clearly say it's the WRONG PERSON or WRONG NUMBER:
        → Say a short apology and goodbye:
            e.g. "I'm sorry for the inconvenience. Goodbye."
        → Then IMMEDIATELY call end_call with reason "wrong_person".

    - After this first confirmation:
    - Do NOT re-introduce yourself again ("I'm Shruti from Digi Loans") later in the call.
    - Do NOT repeatedly confirm their name again unless they themselves are confused.
    """
            },
        )

    context_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    call_ended = asyncio.Event()

    async def _do_end(reason: str):
        logger.info(f"[_do_end] Bot is ending call. Reason: {reason}")
        try:
            await asyncio.sleep(0.5)
            logger.info("[_do_end] Queuing EndFrame...")
            await task.queue_frames([EndFrame()])
            logger.info("[_do_end] EndFrame queued successfully.")
        except Exception as e:
            logger.error(f"[_do_end] Queue EndFrame failed: {e}")

        logger.info("[_do_end] Cancelling PipelineTask...")
        try:
            await task.cancel()
            logger.info("[_do_end] PipelineTask cancelled.")
        except Exception as e:
            logger.error(f"[_do_end] Task cancel error: {e}")
        finally:
            call_ended.set()

    async def end_call_handler(function_name, tool_call_id, args, llm_service, context_obj, result_callback):
        reason = args.get("reason", "unknown")
        logger.info(f"[TOOL] end_call invoked. tool_call_id={tool_call_id}, reason={reason}, args={args}")

        try:
            await result_callback({"status": "call_ended", "reason": reason})
            logger.info("[TOOL] end_call result_callback sent successfully.")
        except Exception as e:
            logger.error(f"[TOOL] end_call result_callback error: {e}")

        await _do_end(reason)

    llm.register_function("end_call", end_call_handler)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport_obj, client):
        logger.info(
            f"Call connected with customer: "
            f"{customer_name if customer_name else 'Unknown'}. Waiting for user to speak first..."
        )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport_obj, client):
        logger.info("Client disconnected, cancelling task.")
        await task.cancel()
        call_ended.set()

    async def goodbye_watcher():
        logger.info("[WATCHER] Goodbye watcher started.")
        last_seen_len = 0

        while not call_ended.is_set():
            try:
                if len(context.messages) == last_seen_len:
                    await asyncio.sleep(0.5)
                    continue

                last_seen_len = len(context.messages)

                last_text = ""
                for m in reversed(context.messages):
                    if m.get("role") == "assistant":
                        c = m.get("content", "")
                        if isinstance(c, str) and c.strip():
                            last_text = c
                            break

                logger.info(f"[WATCHER] Last assistant message: {last_text!r}")

                if not last_text:
                    await asyncio.sleep(0.5)
                    continue

                text_lower = last_text.lower()
                reason = None
                if NOT_INTERESTED_RE.search(text_lower):
                    reason = "customer_not_interested"
                    logger.info("[WATCHER] NOT_INTERESTED_RE matched assistant text.")
                elif GOODBYE_RE.search(text_lower):
                    reason = "customer_goodbye"
                    logger.info("[WATCHER] GOODBYE_RE matched assistant text.")

                if reason and not call_ended.is_set():
                    logger.info(f"[WATCHER] Triggering _do_end with reason={reason}")
                    await _do_end(reason)
                    break

                await asyncio.sleep(0.5)

            except Exception as e:
                logger.warning(f"[WATCHER] Goodbye watcher error: {e}")
                await asyncio.sleep(0.5)

        logger.info("[WATCHER] Goodbye watcher exiting (call_ended or pipeline finished).")

    asyncio.create_task(goodbye_watcher())

    runner = PipelineRunner(handle_sigint=handle_sigint)
    await runner.run(task)
    logger.info("PipelineRunner finished for this call.")


async def bot(runner_args: RunnerArguments):
    # ✅ Pop the next outbound call context (set by /start)
    call_context = await pop_next_outbound_call()
    customer_name = ""
    phone_number = ""

    if call_context:
        customer_name = call_context.get("customer_name", "").strip()
        phone_number = call_context.get("phone_number", "").strip()
        logger.info(
            f"[CALL_MEMORY] Using outbound call context from memory: "
            f"phone_number={phone_number!r}, customer_name={customer_name!r}"
        )
    else:
        logger.info("[CALL_MEMORY] No outbound call context found in memory; customer_name will be 'Unknown'.")

    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    logger.info(f"Auto-detected transport: {transport_type}")
    logger.info(f"Call data from Exotel: {call_data}")

    serializer = ExotelFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
    )

    # ✅ Relaxed VAD a bit to make it easier to trigger on normal speech
    vad_params = VADParams(
        confidence=0.6,
        start_secs=0.25,
        stop_secs=0.5,
        min_volume=0.3,
    )

    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(params=vad_params),
            serializer=serializer,
        ),
    )

    handle_sigint = runner_args.handle_sigint
    await run_bot(transport, handle_sigint, customer_name)
