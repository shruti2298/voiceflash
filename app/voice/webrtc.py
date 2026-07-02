# app/voice/webrtc.py
import asyncio

from fastapi import APIRouter, Request
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from app.voice.bot import default_transport_params, run_bot

router = APIRouter()


@router.post("/offer")
async def offer(request: Request):
    body = await request.json()
    player_name = body.get("player_name", "Player")
    session_id = body.get("session_id")

    connection = SmallWebRTCConnection()
    await connection.initialize(sdp=body["sdp"], type=body["type"])
    transport = SmallWebRTCTransport(webrtc_connection=connection, params=default_transport_params())
    answer = connection.get_answer()   # synchronous in this pipecat version; returns a dict

    # run the bot for this connection in the background. Passing session_id
    # lets the bot resume the session the frontend already created via
    # POST /api/sessions, instead of starting a second, disconnected one that
    # the UI's polling would never see.
    asyncio.create_task(run_bot(transport, player_name, session_id))
    return answer   # {"sdp": ..., "type": "answer", "pc_id": ...}
