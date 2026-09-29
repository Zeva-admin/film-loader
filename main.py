import asyncio
import base64
import contextlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    import yt_dlp
except ImportError:  # Docker installs it; the API can still start locally without it.
    yt_dlp = None


REMOTE = os.getenv("REMOTE", "gdrive_model3m:films")  # remote:папка
MAX_GB = float(os.getenv("MAX_GB", "20"))                  # лимит размера файла
CONF = Path(tempfile.gettempdir()) / "rclone.conf"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
EXT = {
    "video/x-matroska": ".mkv",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    "video/quicktime": ".mov",
    "application/x-subrip": ".srt",
}
VALID_MODES = {"auto", "direct", "smart"}

app = FastAPI(title="Film Loader API", version="2.0")
jobs: dict[str, dict] = {}
queue: asyncio.Queue[str] = asyncio.Queue()
procs: dict[str, asyncio.subprocess.Process] = {}


class UrlIn(BaseModel):
    url: str
    mode: Literal["auto", "direct", "smart"] = "auto"


class JobIn(BaseModel):
    url: str
    title: str = ""
    mode: Literal["auto", "direct", "smart"] = "auto"


class JobCancelled(Exception):
    pass


def clean_title(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", value or "")
    value = re.sub(r"\s+", " ", value).strip().strip(".")
    return value[:180] or "Без названия"


def title_from_name(name: str) -> str:
    return Path(name or "video").stem or "video"


def valid_url(url: str) -> str:
    url = unquote((url or "").strip())
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Нужна ссылка, начинающаяся с http:// или https://")
    return url


async def direct_probe(url: str) -> dict:
    """Fast probe for a real downloadable file."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15,
            headers={"User-Agent": UA},
        ) as client:
            async with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
                headers = response.headers
                status = response.status_code
                final_url = str(response.url)
                if status >= 400:
                    return {
                        "ok": False,
                        "warning": f"Сервер ответил кодом {status}",
                        "name": "file",
                        "title": "file",
                        "ext": "",
                        "size": 0,
                        "type": "неизвестно",
                        "host": urlparse(final_url).hostname,
                        "resumable": False,
                        "method": "direct",
                    }

                size = 0
                content_range = headers.get("content-range", "")
                if status == 206 and "/" in content_range:
                    tail = content_range.split("/")[-1]
                    size = int(tail) if tail.isdigit() else 0
                elif status == 200:
                    length = headers.get("content-length") or "0"
                    size = int(length) if length.isdigit() else 0

                match = re.search(
                    r"filename\*?=(?:UTF-8'')?\"?([^\";]+)",
                    headers.get("content-disposition", ""),
                    re.I,
                )
                name = unquote(match.group(1)) if match else Path(urlparse(final_url).path).name
                name = name or "file"
                content_type = headers.get("content-type", "").split(";")[0].strip().lower()
                ext = Path(name).suffix.lower() or EXT.get(content_type) or mimetypes.guess_extension(content_type) or ""
                is_page = content_type in {"text/html", "application/xhtml+xml"}
                warning = ""
                if is_page:
                    warning = "Это веб-страница, пробую найти в ней видеоплеер и поток."
                elif size > MAX_GB * 1024**3:
                    warning = f"Файл больше лимита {MAX_GB:g} ГБ."
                elif not size:
                    warning = "Размер неизвестен, прогресс будет приблизительным."

                return {
                    "ok": not is_page and size <= MAX_GB * 1024**3,
                    "warning": warning,
                    "name": name,
                    "title": title_from_name(name),
                    "ext": ext,
                    "size": size,
                    "type": content_type or "неизвестно",
                    "host": urlparse(final_url).hostname,
                    "resumable": status == 206 or headers.get("accept-ranges") == "bytes",
                    "method": "direct",
                }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "warning": f"Прямая проверка не удалась: {str(exc)[:180]}",
            "name": "file",
            "title": "file",
            "ext": "",
            "size": 0,
            "type": "неизвестно",
            "host": urlparse(url).hostname,
            "resumable": False,
            "method": "direct",
        }


def extract_info_sync(url: str) -> dict:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp не установлен")
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "http_headers": {"User-Agent": UA},
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if entry]
        info = entries[0] if entries else info
    if not info:
        raise RuntimeError("yt-dlp не нашёл видео на странице")
    return info


async def smart_probe(url: str) -> dict:
    """Use yt-dlp to inspect supported players, pages and streaming services."""
    try:
        info = await asyncio.to_thread(extract_info_sync, url)
        title = clean_title(info.get("title") or info.get("fulltitle") or "video")
        size = info.get("filesize") or info.get("filesize_approx") or 0
        return {
            "ok": size <= MAX_GB * 1024**3 if size else True,
            "warning": f"Найдено через {info.get('extractor_key') or 'yt-dlp'}.",
            "name": f"{title}.mp4",
            "title": title,
            "ext": ".mp4",
            "size": size,
            "type": "video",
            "host": urlparse(info.get("webpage_url") or url).hostname,
            "resumable": True,
            "method": "yt-dlp",
            "extractor": info.get("extractor_key") or info.get("extractor"),
            "duration": info.get("duration"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "warning": f"Не удалось найти видео через yt-dlp: {str(exc)[:220]}",
            "name": "video",
            "title": "video",
            "ext": ".mp4",
            "size": 0,
            "type": "неизвестно",
            "host": urlparse(url).hostname,
            "resumable": False,
            "method": "yt-dlp",
        }


async def inspect_url(url: str, mode: str = "auto") -> dict:
    url = valid_url(url)
    if mode not in VALID_MODES:
        raise HTTPException(400, "mode должен быть auto, direct или smart")

    if mode == "direct":
        return await direct_probe(url)

    if mode == "smart":
        smart = await smart_probe(url)
        if smart["ok"]:
            return smart
        direct = await direct_probe(url)
        return direct if direct["ok"] else smart

    direct = await direct_probe(url)
    if direct["ok"]:
        return direct
    smart = await smart_probe(url)
    if smart["ok"]:
        return smart
    direct["warning"] = f"{direct['warning']} {smart['warning']}".strip()
    return direct


async def enqueue(url: str, title: str = "", mode: str = "auto") -> dict:
    url = valid_url(url)
    info = await inspect_url(url, mode)
    if not info["ok"]:
        raise HTTPException(400, info["warning"])

    title = clean_title(title or info.get("title") or title_from_name(info.get("name", "video")))
    ext = info.get("ext") or (".mp4" if info.get("method") == "yt-dlp" else "")
    jid = uuid.uuid4().hex[:8]
    jobs[jid] = {
        "id": jid,
        "url": url,
        "title": title,
        "filename": title + ext,
        "size": info.get("size", 0),
        "total": info.get("size", 0),
        "done": 0,
        "speed": 0,
        "eta": None,
        "status": "queued",
        "stage": "В очереди",
        "error": "",
        "created": time.time(),
        "method": info.get("method", "direct"),
        "extractor": info.get("extractor", ""),
        "source_name": info.get("name", ""),
        "duration": info.get("duration"),
    }
    await queue.put(jid)
    return public(jobs[jid])


def public(job: dict) -> dict:
    return {key: value for key, value in job.items() if key not in {"url", "tmp_dir"}}


@app.post("/api/info")
async def info(body: UrlIn):
    return await inspect_url(body.url, body.mode)


@app.get("/api/info")
async def info_get(url: str, mode: str = "auto"):
    return await inspect_url(url, mode)


@app.post("/api/jobs")
async def add_job(body: JobIn):
    return await enqueue(body.url, body.title, body.mode)


@app.get("/api/download")
async def download_get(url: str, title: str = "", mode: str = "auto"):
    """CLI-friendly endpoint: curl -G ... --data-urlencode url=..."""
    return await enqueue(url, title, mode)


@app.get("/api/jobs")
async def list_jobs():
    return [public(job) for job in sorted(jobs.values(), key=lambda item: -item["created"])[:30]]


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return public(job)


@app.post("/api/jobs/{jid}/cancel")
async def cancel(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    if job["status"] in ("queued", "running"):
        job["status"] = "cancelled"
        job["stage"] = "Отменено"
        process = procs.get(jid)
        if process:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
    return public(job)


@app.get("/health")
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "queue": queue.qsize(),
        "yt_dlp": yt_dlp is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "rclone": shutil.which("rclone") is not None,
        "remote": REMOTE,
    }


def rclone_command(action: str, source: str, destination: str) -> list[str]:
    return [
        "rclone",
        action,
        source,
        destination,
        "--config",
        str(CONF),
        "--transfers",
        "1",
        "--drive-chunk-size",
        "16M",
        "--buffer-size",
        "8M",
        "--retries",
        "3",
        "--low-level-retries",
        "10",
        "--user-agent",
        UA,
        "--stats",
        "1s",
        "--use-json-log",
        "-v",
    ]


async def run_rclone(job: dict, action: str, source: str, destination: str) -> None:
    process = await asyncio.create_subprocess_exec(
        *rclone_command(action, source, destination),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    procs[job["id"]] = process
    try:
        async for raw_line in process.stderr:
            try:
                event = json.loads(raw_line.decode(errors="replace"))
            except (ValueError, UnicodeDecodeError):
                continue
            stats = event.get("stats")
            if stats:
                job["done"] = stats.get("bytes", job["done"])
                job["speed"] = stats.get("speed", 0)
                job["eta"] = stats.get("eta")
                job["total"] = stats.get("totalBytes") or job["total"]
            elif event.get("level") in ("error", "critical"):
                job["error"] = str(event.get("msg", ""))[:300]
        return_code = await process.wait()
    finally:
        procs.pop(job["id"], None)

    if job["status"] == "cancelled":
        raise JobCancelled()
    if return_code != 0:
        raise RuntimeError(job.get("error") or f"rclone завершился с кодом {return_code}")


def ytdlp_hook(job: dict, data: dict) -> None:
    if job["status"] == "cancelled":
        raise JobCancelled()
    if data.get("status") == "downloading":
        job["stage"] = "Поиск и скачивание видео"
        job["done"] = data.get("downloaded_bytes") or 0
        job["total"] = data.get("total_bytes") or data.get("total_bytes_estimate") or job["total"]
        job["size"] = job["total"] or job["size"]
        job["speed"] = data.get("speed") or 0
        job["eta"] = data.get("eta")
    elif data.get("status") == "finished":
        job["stage"] = "Объединение потоков / подготовка"
        job["done"] = job["total"] or job["done"]
        job["speed"] = 0
        job["eta"] = None


def download_with_ytdlp(job: dict, tmp_dir: Path) -> None:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp не установлен в контейнере")

    output = tmp_dir / f"{job['title']}.%(ext)s"
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bv*+ba/best",
        "merge_output_format": "mp4",
        "outtmpl": str(output),
        "continuedl": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {"User-Agent": UA},
        "progress_hooks": [lambda data: ytdlp_hook(job, data)],
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([job["url"]])


async def run_smart_download(job: dict) -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"film-loader-{job['id']}-"))
    try:
        job["stage"] = "Поиск видеопотока"
        await asyncio.to_thread(download_with_ytdlp, job, tmp_dir)
        if job["status"] == "cancelled":
            raise JobCancelled()

        files = [
            path
            for path in tmp_dir.iterdir()
            if path.is_file() and not path.name.endswith((".part", ".ytdl", ".json"))
        ]
        if not files:
            raise RuntimeError("yt-dlp завершился без выходного видеофайла")
        source = max(files, key=lambda path: path.stat().st_size)
        actual_size = source.stat().st_size
        if actual_size > MAX_GB * 1024**3:
            raise RuntimeError(f"Файл больше лимита {MAX_GB:g} ГБ")
        job["size"] = actual_size
        job["total"] = actual_size
        job["done"] = 0
        job["filename"] = f"{job['title']}{source.suffix.lower() or '.mp4'}"
        job["stage"] = "Загрузка на Google Drive"
        await run_rclone(job, "copyto", str(source), f"{REMOTE}/{job['filename']}")
    except JobCancelled:
        raise
    except Exception as exc:
        if job["status"] == "cancelled":
            raise JobCancelled()
        raise RuntimeError(str(exc)[:300]) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def worker():
    while True:
        jid = await queue.get()
        job = jobs.get(jid)
        try:
            if not job or job["status"] != "queued":
                continue
            job["status"] = "running"
            job["stage"] = "Загрузка файла" if job["method"] == "direct" else "Поиск видеопотока"
            if job["method"] == "direct":
                await run_rclone(job, "copyurl", job["url"], f"{REMOTE}/{job['filename']}")
            else:
                await run_smart_download(job)

            if job["status"] == "cancelled":
                continue
            job["status"] = "done"
            job["stage"] = "Готово"
            job["done"] = job["size"] or job["done"]
            job["total"] = job["size"] or job["total"]
            job["eta"] = 0
            job["speed"] = 0
        except JobCancelled:
            if job:
                job["status"] = "cancelled"
                job["stage"] = "Отменено"
        except Exception as exc:
            if job:
                job["status"] = "failed"
                job["stage"] = "Ошибка"
                job["error"] = str(exc)[:300]
        finally:
            queue.task_done()


@app.on_event("startup")
async def startup():
    encoded = os.getenv("rclone") or os.getenv("RCLONE_CONFIG_B64")
    if encoded:
        CONF.write_bytes(base64.b64decode(encoded))
    app.state.worker = asyncio.create_task(worker())


@app.on_event("shutdown")
async def shutdown():
    task = getattr(app.state, "worker", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@app.get("/api/{source:path}")
async def quick_download(source: str, title: str = "", mode: str = "auto"):
    """Short curl form: /api/https://example.com/video or URL-encoded source."""
    source = unquote(source)
    if source.startswith(("http:/", "https:/")) and not source.startswith(("http://", "https://")):
        source = source.replace(":/", "://", 1)
    if not source.startswith(("http://", "https://")):
        raise HTTPException(400, "Укажите ссылку после /api/ или используйте /api/download?url=...")
    return await enqueue(source, title, mode)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
