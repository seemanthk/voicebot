# server.py
import os
from contextlib import asynccontextmanager
import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from call_memory import add_outbound_call  # ✅ NEW

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

logger.add(
    os.path.join(LOG_DIR, "server.log"),
    rotation="10 MB",
    retention="7 days",
    compression="zip",
    enqueue=True,
    backtrace=True,
    diagnose=True,
    level="INFO",
)

load_dotenv(override=True)


async def make_exotel_call(
    session: aiohttp.ClientSession,
    to_number: str,
    from_number: str,
    customer_name: str | None = "",
):
    """Make an outbound call using Exotel's Connect API."""
    api_key = os.getenv("EXOTEL_API_KEY")
    api_token = os.getenv("EXOTEL_API_TOKEN")
    sid = os.getenv("EXOTEL_SID")

    if not all([api_key, api_token, sid]):
        raise ValueError("Missing Exotel credentials: EXOTEL_API_KEY, EXOTEL_API_TOKEN, EXOTEL_SID")

    url = f"https://api.exotel.com/v1/Accounts/{sid}/Calls/connect"

    data = {
        "From": from_number,
        "To": to_number,
        "CallerId": from_number,
        "CallType": "trans",
    }

    if customer_name:
        # Even if Media Streams doesn't echo this yet, keep it for future.
        data["CustomField"] = customer_name
        logger.info(f"[EXOTEL] Sending CustomField with customer_name={customer_name!r}")

    auth = aiohttp.BasicAuth(api_key, api_token)

    async with session.post(url, data=data, auth=auth) as response:
        if response.status != 200:
            error_text = await response.text()
            raise Exception(f"Exotel API error ({response.status}): {error_text}")

        result_text = await response.text()
        logger.debug(f"[EXOTEL] Raw XML response: {result_text[:500]}")

        call_sid = "unknown"
        if "<Sid>" in result_text:
            start = result_text.find("<Sid>") + 5
            end = result_text.find("</Sid>")
            if end > start:
                call_sid = result_text[start:end]

        logger.info(f"[EXOTEL] Outbound call initiated. Exotel Sid={call_sid}")
        return {"status": "call_initiated", "call_sid": call_sid}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.session = aiohttp.ClientSession()
    yield
    await app.state.session.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # open for testing
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/start")
async def initiate_outbound_call(request: Request) -> JSONResponse:
    """Handle outbound call request and initiate call via Exotel."""
    logger.info("Received outbound call request")

    try:
        data = await request.json()

        dialout = data.get("dialout_settings")
        if not dialout:
            raise HTTPException(status_code=400, detail="Missing 'dialout_settings' in the request body")

        phone_number = str(dialout.get("phone_number", "")).strip()
        if not phone_number:
            raise HTTPException(status_code=400, detail="Missing 'phone_number' in dialout_settings")

        customer_name = dialout.get("customer_name", "").strip()

        logger.info(f"Processing outbound call to {phone_number}, customer_name={customer_name!r}")

        try:
            call_result = await make_exotel_call(
                session=request.app.state.session,
                to_number=phone_number,
                from_number=os.getenv("EXOTEL_PHONE_NUMBER"),
                customer_name=customer_name,
            )
            call_sid = call_result.get("call_sid", "unknown")
            logger.info(f"make_exotel_call returned call_sid={call_sid}")

            # ✅ Store this call context for the next /ws connection
            await add_outbound_call(
                {
                    "phone_number": phone_number,
                    "customer_name": customer_name,
                }
            )
            logger.info(
                f"[CALL_MEMORY] Stored outbound call context for phone={phone_number}, "
                f"customer_name={customer_name!r}"
            )

        except Exception as e:
            logger.error(f"Error initiating Exotel call: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to initiate call: {str(e)}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

    return JSONResponse(
        {
            "call_sid": call_sid,
            "status": "call_initiated",
            "phone_number": phone_number,
            "customer_name": customer_name,
        }
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket connection from Exotel Media Streams."""
    await websocket.accept()
    logger.info("WebSocket connection accepted for outbound call")

    try:
        from bot import bot
        from pipecat.runner.types import WebSocketRunnerArguments

        runner_args = WebSocketRunnerArguments(websocket=websocket)
        runner_args.handle_sigint = False

        await bot(runner_args)

    except Exception as e:
        logger.error(f"Error in WebSocket endpoint: {e}")
    finally:
        try:
            logger.info(f"[WS] Finalizing websocket, state={websocket.client_state.name}")
            if websocket.client_state.name != "DISCONNECTED":
                logger.info("[WS] WebSocket still open, closing from server side...")
                await websocket.close()
            else:
                logger.info("[WS] WebSocket already disconnected, nothing to close.")
        except Exception as e:
            logger.warning(f"[WS] Error closing WebSocket: {e}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
