"""仅供本地开发验证的模拟 provider，不连接任何付费 API。"""

import argparse
import asyncio

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
calls: list[dict] = []
names = [
    "demo_flash_small",
    "demo-pro-preview",
    "demo-unavailable",
    "demo-rate-limited",
    "demo-empty",
    "demo-slow",
    "text-embedding-3-small",
]


@app.get("/{version}/models")
async def list_models(version: str):
    if version == "v1beta":
        return {
            "models": [
                {
                    "name": f"models/{name}",
                    "displayName": name,
                    "supportedGenerationMethods": [
                        "embedContent" if "embedding" in name else "generateContent"
                    ],
                }
                for name in names
            ]
        }
    return {"data": [{"id": name} for name in names], "has_more": False}


@app.get("/_requests")
async def requests():
    return {"calls": calls}


@app.post("/{version}/{path:path}")
async def generate(version: str, path: str, request: Request):
    body = await request.json()
    model = body.get("model") or path.removeprefix("models/").split(":", 1)[0]
    calls.append({"path": f"/{version}/{path}", "model": model})
    if "unavailable" in model:
        return JSONResponse({"error": {"message": "当前密钥没有模型访问权限"}}, status_code=403)
    if "rate-limited" in model:
        return JSONResponse({"error": {"message": "模拟速率限制，请稍后重试"}}, status_code=429)
    await asyncio.sleep(10 if "slow" in model else 0.2)
    text = "" if "empty" in model else "hi"
    if path == "chat/completions":
        return {"model": model, "choices": [{"message": {"content": text}}]}
    if path == "responses":
        return {
            "model": model,
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        }
    if path == "messages":
        return {"model": model, "content": [{"type": "text", "text": text}]}
    if path.endswith(":generateContent"):
        return {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    return JSONResponse({"error": {"message": "未知协议路径"}}, status_code=404)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="启动本地模拟 provider")
    parser.add_argument("--port", type=int, default=9876)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
