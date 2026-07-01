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

    connection = SmallWebRTCConnection()
    await connection.initialize(sdp=body["sdp"], type=body["type"])
    transport = SmallWebRTCTransport(webrtc_connection=connection, params=default_transport_params())
    answer = connection.get_answer()   # synchronous in this pipecat version; returns a dict

    # run the bot for this connection in the background
    asyncio.create_task(run_bot(transport, player_name))
    return answer   # {"sdp": ..., "type": "answer", "pc_id": ...}
