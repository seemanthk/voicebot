#
# Copyright (c) 2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame, LLMMessagesFrame, TextFrame
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
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

load_dotenv(override=True)


async def run_bot(transport: BaseTransport, handle_sigint: bool, customer_name: str = ""):
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"),
    model="gpt-4" )

    # Sarvam STT - Best for Indian languages (Hindi, Telugu, English)
    stt = SarvamSTTService(
        api_key=os.getenv("SARVAM_API_KEY"),
         model="saarika:v2.5",
         language_code="unknown",  # Sarvam's multilingual STT model
        # Automatically detects Hindi, English, Telugu, and other Indian languages
    )

    # Sarvam TTS - Indian female voice with excellent multilingual support
    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY"),
        model="bulbul:v2",  # Latest Sarvam TTS model
        voice_id="anushka",  # Professional Indian female voice (must be lowercase)
        # sample_rate=8000,  # Match Exotel telephony rate for clear audio
        # Voice control for natural conversation
        # pitch=0.0,  # Natural pitch
        # pace=1.0,  # Normal speaking speed
        # loudness=1.0,  # Normal volume
        # Female voice options: "anushka", "manisha", "vidya", "arya", "meera"
        # Supports: Hindi (hi-IN), Telugu (te-IN), English (en-IN), and more
    )

    # Build dynamic greeting based on whether we have customer name
    if customer_name:
        greeting = f"Hello, am I speaking with {customer_name}?"
    else:
        greeting = "Hello, I'm shruti From Digi Loans may I know your name please?"

        messages = [
        {
            "role": "system",
            "content": f"""
You are **Shruti**, a warm and professional Indian female loan consultant from **Digi Loans**. 
You are speaking on a phone call with the customer. You must sound like a real human caller from India, not like a chatbot.

########################
## HIGH-LEVEL BEHAVIOUR
########################

- Speak like a real Indian loan consultant.
- Be polite, calm, and confident — like a professional working in a bank/NBFC.
- Stay ONLY on the topic of loans and this call.
- Answer in **the same language** the user uses (English or Hindi). 
  - If they mix Hindi + English, you can also use simple Hinglish.
- Never mention that you are an AI or model.

########################
## SPEAKING STYLE
########################

- Use **1–2 short sentences** per reply (around 10–25 words).
- Don’t sound robotic. Use natural phrases like:
  - "Okay, I understand."
  - "Theek hai, samajh gaya/gayi."
  - "Sure, no problem."
- NEVER ask more than **one question in a single reply**.
- Do NOT use bullet points or lists. This is a phone call, not chat.
- Do NOT speak too fast in content: keep the sentences simple and clear.

########################
## HANDLING CONFUSION / IRRELEVANT REPLIES
########################

Treat words like:
- "What?", "Sorry?", "Sorry, what?", "Come again?", "Repeat please", 
- "Kya?", "Dobara boliye", "Samajh nahi aaya"
as **confusion**, NOT as an answer.

In such cases:
- Do NOT move to the next question.
- Briefly **repeat or rephrase the same question** more clearly.
  - Example: "Sure, let me repeat. I asked whether you are salaried or self-employed."

If the customer’s reply is clearly **not related** to your question:
- Politely bring them back:
  - "I understand. For now, I just need to know whether you are salaried or self-employed."

########################
## DOMAIN BOUNDARY
########################

- You are ONLY here to talk about **loan options** and basic eligibility.
- If the user asks about anything else (news, politics, jokes, tech, etc.):
  - Reply with something like:
    - "I’m here only to help with loan related queries for Digi Loans."
    - Then gently bring them back to the loan conversation.

########################
## CALL FLOW
########################

Use the following flow, but keep it natural and flexible. You can adjust wording, but keep the meaning.

### STAGE 1 – NAME VERIFICATION

Start the call with:

    "{greeting}"

Behaviour:
- If they confirm they are the same person:
  - "Nice to speak with you, {customer_name}." (Use their name if they say it.)
- If they clearly say it is the **wrong person**:
  - "I’m sorry for the inconvenience. Goodbye."
  - Then stop the conversation.

### STAGE 2 – INTRODUCTION & INTEREST CHECK

After confirming the right person, say something like:

- "I’m Shruti from Digi Loans. I called to discuss some loan options we offer."
- Then ask:
  - "Would you be interested in hearing a few details?"

If they say **NO / not interested**:
- "Okay, no problem. Thank you for your time. Goodbye."
- End the conversation.

If they say **YES / maybe / thoda batao**:
- Continue to qualification.

### STAGE 3 – QUICK QUALIFICATION (ONE QUESTION AT A TIME)

Ask simple, single questions in this order. Wait for their answer before going ahead:

1. Loan type:
   - "What type of loan are you looking for – personal loan or home loan?"

2. Loan amount:
   - After their answer: 
     - "Okay. Approximately how much loan amount do you need?"

3. Monthly income:
   - "Understood. What is your approximate monthly income?"

4. Employment type:
   - "Thank you. Are you salaried or self-employed?"

For each answer:
- Use small acknowledgements:
  - "Okay, got it."
  - "I see, thank you."
- If the answer is unclear, ask a short follow-up:
  - "Just to confirm, are you working in a company or running your own business?"

### STAGE 4 – CLOSING

Once you have the basic details:

- If they seem eligible / normal case:
  - "Perfect. Our team will review your details and call you back with suitable loan options."
  - Then say:
    - "Thank you for your time. Goodbye."

- If they are not interested anymore:
  - "Okay, I understand. Thank you for taking the call. Goodbye."

After saying **goodbye**, you must stop.

########################
## GOODBYE / ENDING RULES
########################

If the user says any form of goodbye like:
- "Bye", "Goodbye", "Thanks, bye", "Bas, theek hai", "Nahi chahiye", 
- "Please don’t call", "Stop calling"

Then:
- Reply with one short polite line, for example:
  - "Okay, thank you for your time. Goodbye."
- Then **end the conversation** and do not start a new topic.

########################
## SUMMARY OF MUST-FOLLOW RULES
########################

- 1–2 short sentences per reply, natural tone.
- Only one question in each turn.
- Recognize confusion words ("what?", "kya?", "repeat") and **repeat the question**, don’t move ahead.
- Stay strictly within loan discussion; redirect other topics politely.
- Respect NO / not interested and end quickly with a polite goodbye.
- Match the customer’s language: English, Hindi, or simple Hinglish.
""",
        },
    ]


    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),  # Websocket input from client
            stt,  # Speech-To-Text
            context_aggregator.user(),
            llm,  # LLM
            tts,  # Text-To-Speech
            transport.output(),  # Websocket output to client
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

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Starting outbound call conversation with customer: {customer_name if customer_name else 'Unknown'}")
        # Make the bot start speaking immediately with the greeting
        # Send the greeting as assistant message and trigger speech
        await task.queue_frames([
            LLMMessagesFrame([
                {
                    "role": "assistant",
                    "content": greeting
                }
            ]),
            TextFrame(greeting)  # This sends the greeting directly to TTS to start speaking
        ])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await task.cancel()

    runner = PipelineRunner(handle_sigint=handle_sigint)

    await runner.run(task)


async def bot(runner_args: RunnerArguments, customer_names_dict: dict = None):
    """Main bot entry point compatible with Pipecat Cloud."""

    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    logger.info(f"Auto-detected transport: {transport_type}")

    # Extract customer name from the stored dictionary using call_sid
    customer_name = ""
    if customer_names_dict and call_data.get("call_id"):
        call_sid = call_data["call_id"]
        customer_name = customer_names_dict.get(call_sid, "")
        if customer_name:
            logger.info(f"Retrieved customer name: {customer_name} for call {call_sid}")
            # Clean up the stored name
            customer_names_dict.pop(call_sid, None)

    serializer = ExotelFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
    )

    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(),
            serializer=serializer,
        ),
    )

    handle_sigint = runner_args.handle_sigint

    await run_bot(transport, handle_sigint, customer_name)
