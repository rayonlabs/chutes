import json
import asyncio
import os
from pathlib import Path
from typing import Optional, List, AsyncGenerator
from datetime import datetime
import aiofiles
from fastapi import FastAPI, Query, HTTPException, Request, Depends, Response
from fastapi.responses import StreamingResponse
from chutes.entrypoint._shared import authenticate_request
import uvicorn

LOG_BASE = os.getenv("LOG_BASE", "/tmp/_chute.log")
POLL_INTERVAL = 1.0

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.dev = False


def get_available_logs() -> List[str]:
    """
    Get list of available log files.
    """
    logs = []
    if Path(LOG_BASE).exists():
        logs.append("current")
    for i in range(1, 5):
        if Path(f"{LOG_BASE}.{i}").exists():
            logs.append(str(i))
    return logs


async def verify_auth(request: Request):
    """
    Dependency to verify authentication for all endpoints.
    """
    if app.dev:
        return await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    body_bytes, error_response = await authenticate_request(request)
    if error_response:
        raise HTTPException(
            status_code=error_response.status_code, detail=json.loads(error_response.body)
        )
    return body_bytes


def get_log_path(filename: str) -> Path:
    """
    Convert filename parameter to actual path.
    """
    if filename == "current":
        return Path(LOG_BASE)
    elif filename in ["1", "2", "3", "4"]:
        return Path(f"{LOG_BASE}.{filename}")
    else:
        raise ValueError(f"Invalid filename: {filename}")


async def read_last_n_lines(filepath: Path, n: Optional[int] = None) -> List[str]:
    """
    Read last n lines from a file asynchronously.
    """
    if not filepath.exists():
        return []
    try:
        async with aiofiles.open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            if n is None:
                content = await f.read()
                return content.splitlines()
            else:
                async with aiofiles.open(filepath, "rb") as fb:
                    await fb.seek(0, 2)
                    file_length = await fb.tell()
                    if file_length == 0:
                        return []
                    buffer = bytearray()
                    lines_found = 0
                    position = file_length
                    while lines_found < n and position > 0:
                        chunk_size = min(4096, position)
                        position -= chunk_size
                        await fb.seek(position)
                        chunk = await fb.read(chunk_size)
                        buffer = chunk + buffer
                        lines_found = buffer.count(b"\n")
                    text = buffer.decode("utf-8", errors="ignore")
                    all_lines = text.splitlines()
                    return all_lines[-n:] if len(all_lines) > n else all_lines
    except Exception as e:
        print(f"Error reading file: {e}")
        return []


@app.get("/")
async def root(auth: bytes = Depends(verify_auth)):
    """
    Root endpoint - provides API information.
    """
    return {
        "service": "Log Streaming API",
        "endpoints": {
            "/logs": "List available log files",
            "/logs/read/{filename}": "Read log file contents",
            "/logs/stream": "Stream current log file via SSE",
        },
    }


@app.get("/logs")
async def list_logs(auth: bytes = Depends(verify_auth)):
    """
    List available log files.
    """
    logs = get_available_logs()
    log_info = []
    for log in logs:
        path = get_log_path(log)
        if path.exists():
            stat = path.stat()
            log_info.append(
                {
                    "name": log,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "path": str(path),
                }
            )
    return {"logs": log_info}


@app.get("/logs/read/{filename}")
async def read_log(
    filename: str,
    lines: Optional[int] = Query(None, description="Number of lines to read from end (None = all)"),
    auth: bytes = Depends(verify_auth),
):
    """
    Read contents from a log file and return as plain text.
    """
    try:
        path = get_log_path(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Log file '{filename}' not found")

    log_lines = await read_last_n_lines(path, lines)

    return Response(
        content="\n".join(log_lines),
        media_type="text/plain",
        headers={
            "X-Filename": filename,
            "X-Lines-Returned": str(len(log_lines)),
        },
    )


async def log_streamer(filename: str, backfill: Optional[int] = None) -> AsyncGenerator[str, None]:
    """
    Stream log updates via SSE with proper formatting and flushing.
    """
    try:
        path = get_log_path(filename)
    except ValueError:
        yield f"data: {json.dumps({'error': f'Invalid filename: {filename}'})}\n\n"
        return
    if not path.exists():
        yield f"data: {json.dumps({'error': f'File does not exist: {path}'})}\n\n"
        return

    yield f"data: {json.dumps({'event': 'connected', 'filename': filename})}\n\n"

    if backfill is not None and backfill > 0:
        lines = await read_last_n_lines(path, backfill)
        for line in lines:
            if line.strip():
                yield f"data: {json.dumps({'log': line})}\n\n"

    last_position = 0
    if path.exists():
        last_position = path.stat().st_size

    consecutive_errors = 0
    while True:
        try:
            if not path.exists():
                yield f"data: {json.dumps({'event': 'file_removed', 'filename': filename})}\n\n"
                break

            current_size = path.stat().st_size

            if current_size > last_position:
                async with aiofiles.open(path, "r", encoding="utf-8", errors="ignore") as f:
                    await f.seek(last_position)
                    new_content = await f.read(current_size - last_position)

                    lines = new_content.split("\n")

                    for i, line in enumerate(lines[:-1]):
                        if line.strip():
                            yield f"data: {json.dumps({'log': line})}\n\n"

                    if new_content.endswith("\n") and lines[-1].strip():
                        yield f"data: {json.dumps({'log': lines[-1]})}\n\n"

                    last_position = current_size
                    consecutive_errors = 0

            elif current_size < last_position:
                yield f"data: {json.dumps({'event': 'file_rotated', 'filename': filename})}\n\n"
                last_position = 0
            else:
                if consecutive_errors % 10 == 0:
                    yield ".\n\n"

            await asyncio.sleep(POLL_INTERVAL)

        except asyncio.CancelledError:
            yield f"data: {json.dumps({'event': 'stream_cancelled'})}\n\n"
            break
        except Exception as e:
            consecutive_errors += 1
            yield f"data: {json.dumps({'error': f'Stream error: {str(e)}'})}\n\n"
            if consecutive_errors > 5:
                yield f"data: {json.dumps({'event': 'too_many_errors', 'error': 'Stopping stream due to repeated errors'})}\n\n"
                break
            await asyncio.sleep(POLL_INTERVAL * 2)


@app.get("/logs/stream")
async def stream_log(
    backfill: Optional[int] = Query(
        None, description="Number of recent lines to send before streaming"
    ),
    auth: bytes = Depends(verify_auth),
):
    """
    Stream log updates via SSE.
    """
    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
    }

    return StreamingResponse(
        log_streamer("current", backfill),
        media_type="text/event-stream",
        headers=headers,
    )


@app.get("/test-stream")
async def test_stream():
    """Test endpoint to verify SSE is working"""

    async def generate():
        for i in range(5):
            yield f"data: {json.dumps({'count': i, 'time': datetime.now().isoformat()})}\n\n"
            await asyncio.sleep(1)
        yield f"data: {json.dumps({'event': 'complete'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def launch_server(
    host: str = "0.0.0.0",
    port: int = 8001,
    dev: bool = False,
    certfile: Optional[str] = None,
    keyfile: Optional[str] = None,
):
    """
    Start the logging server using uvicorn.
    """
    if dev:
        app.dev = True

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        limit_concurrency=1000,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        log_level="info",
        access_log=True,
    )
    server = uvicorn.Server(config)
    await server.serve()


def main():
    """
    Entry point for running the server.
    """
    asyncio.run(launch_server())


if __name__ == "__main__":
    main()
