import asyncio
import json
import logging
import traceback
from pydantic import BaseModel

from fastapi import FastAPI, WebSocket, HTTPException, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from diffusers import DiffusionPipeline
import torch
from PIL import Image
import numpy as np
import gradio as gr
import io
import uuid
import os
import time

MAX_QUEUE_SIZE = 4
TIMEOUT = float(os.environ.get("TIMEOUT", 0))
SAFETY_CHECKER = os.environ.get("SAFETY_CHECKER", None)


if SAFETY_CHECKER == "True":
    pipe = DiffusionPipeline.from_pretrained(
        "SimianLuo/LCM_Dreamshaper_v7",
        custom_pipeline="latent_consistency_img2img.py",
        custom_revision="main",
    )
else:
    pipe = DiffusionPipeline.from_pretrained(
        "SimianLuo/LCM_Dreamshaper_v7",
        safety_checker=None,
        custom_pipeline="latent_consistency_img2img.py",
        custom_revision="main",
    )
pipe.to(torch_device="cuda", torch_dtype=torch.float16)
user_queue_map = {}


def predict(input_image, prompt, guidance_scale=8.0, strength=0.5, seed=2159232):
    generator = torch.manual_seed(seed)
    # Can be set to 1~50 steps. LCM support fast inference even <= 4 steps. Recommend: 1~8 steps.
    num_inference_steps = 4
    results = pipe(
        prompt=prompt,
        generator=generator,
        image=input_image,
        strength=strength,
        num_inference_steps=num_inference_steps,
        guidance_scale=guidance_scale,
        lcm_origin_steps=20,
        output_type="pil",
    )
    nsfw_content_detected = (
        results.nsfw_content_detected[0]
        if "nsfw_content_detected" in results
        else False
    )
    if nsfw_content_detected:
        return None
    return results.images[0]


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class InputParams(BaseModel):
    seed: int
    prompt: str
    strength: float
    guidance_scale: float


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    if len(user_queue_map) >= MAX_QUEUE_SIZE:
        print("Server is full")
        await websocket.send_json({"status": "error", "message": "Server is full"})
        await websocket.close()
        return

    try:
        uid = str(uuid.uuid4())
        print(f"New user connected: {uid}")
        await websocket.send_json(
            {"status": "success", "message": "Connected", "userId": uid}
        )
        params = await websocket.receive_json()
        params = InputParams(**params)
        user_queue_map[uid] = {
            "queue": asyncio.Queue(),
            "params": params,
        }
        await handle_websocket_data(websocket, uid)
    except WebSocketDisconnect as e:
        logging.error(f"Error: {e}")
        traceback.print_exc()
    finally:
        print(f"User disconnected: {uid}")
        queue_value = user_queue_map.pop(uid, None)
        queue = queue_value.get("queue", None)
        if queue:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    continue


@app.get("/queue_size")
async def get_queue_size():
    queue_size = len(user_queue_map)
    return JSONResponse({"queue_size": queue_size})


@app.get("/stream/{user_id}")
async def stream(user_id: uuid.UUID):
    uid = str(user_id)
    user_queue = user_queue_map[uid]
    queue = user_queue["queue"]
    params = user_queue["params"]
    seed = params.seed
    prompt = params.prompt
    strength = params.strength
    guidance_scale = params.guidance_scale
    if not queue:
        return HTTPException(status_code=404, detail="User not found")

    async def generate():
        while True:
            input_image = await queue.get()
            if input_image is None:
                continue

            image = predict(input_image, prompt, guidance_scale, strength, seed)
            if image is None:
                continue
            frame_data = io.BytesIO()
            image.save(frame_data, format="JPEG")
            frame_data = frame_data.getvalue()
            if frame_data is not None and len(frame_data) > 0:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame_data + b"\r\n"

            await asyncio.sleep(1.0 / 120.0)

    return StreamingResponse(
        generate(), media_type="multipart/x-mixed-replace;boundary=frame"
    )


async def handle_websocket_data(websocket: WebSocket, user_id: uuid.UUID):
    uid = str(user_id)
    user_queue = user_queue_map[uid]
    queue = user_queue["queue"]
    if not queue:
        return HTTPException(status_code=404, detail="User not found")
    last_time = time.time()
    try:
        while True:
            data = await websocket.receive_bytes()
            pil_image = Image.open(io.BytesIO(data))

            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    continue
            await queue.put(pil_image)
            if TIMEOUT > 0 and time.time() - last_time > TIMEOUT:
                await websocket.send_json( 
                    {
                        "status": "timeout",
                        "message": "Your session has ended",
                        "userId": uid,
                    }
                )
                await websocket.close()
                return

    except Exception as e:
        logging.error(f"Error: {e}")
        traceback.print_exc()


app.mount("/", StaticFiles(directory="public", html=True), name="public")
