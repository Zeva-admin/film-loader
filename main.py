import asyncio, base64, json, mimetypes, os, re, time, uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

REMOTE = os.getenv("REMOTE", "gdrive_model3m:films")  # remote:папка
MAX_GB = float(os.getenv("MAX_GB", "20"))               # лимит размера файла
CONF = Path("/tmp/rclone.conf")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
EXT = {"video/x-matroska": ".mkv", "video/mp4": ".mp4", "video/webm": ".webm",
       "video/x-msvideo": ".avi", "video/quicktime": ".mov", "application/x-subrip": ".srt"}

app = FastAPI()
jobs: dict[str, dict] = {}
queue: asyncio.Queue = asyncio.Queue()
procs: dict[str, asyncio.subprocess.Process] = {}


class UrlIn(BaseModel):
    url: str

class JobIn(BaseModel):
    url: str
    title: str


async def probe(url: str) -> dict:
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Нужна ссылка, начинающаяся с http:// или https://")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15, headers={"User-Agent": UA}) as c:
            async with c.stream("GET", url, headers={"Range": "bytes=0-0"}) as r:
                if r.status_code >= 400:
                    raise HTTPException(400, f"Сервер ответил кодом {r.status_code}")
                h, code, final = r.headers, r.status_code, str(r.url)
    except httpx.HTTPError as e:
        raise HTTPException(400, f"Не удалось открыть ссылку: {e}")

    size = 0
    if code == 206 and "/" in h.get("content-range", ""):
        tail = h["content-range"].split("/")[-1]
        size = int(tail) if tail.isdigit() else 0
    elif code == 200:
        size = int(h.get("content-length") or 0)

    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", h.get("content-disposition", ""), re.I)
    name = unquote(m.group(1)) if m else unquote(Path(urlparse(final).path).name) or "file"
    ctype = h.get("content-type", "").split(";")[0].strip()
    ext = Path(name).suffix.lower() or EXT.get(ctype) or mimetypes.guess_extension(ctype) or ""

    warning, ok = "", True
    if ctype == "text/html":
        warning, ok = "Это веб-страница, а не файл. Нужна прямая ссылка на скачивание.", False
    elif size > MAX_GB * 1024**3:
        warning, ok = f"Файл больше лимита {MAX_GB:g} ГБ.", False
    elif not size:
        warning = "Размер неизвестен, прогресс будет приблизительным."
    return dict(ok=ok, warning=warning, name=name, ext=ext, size=size, type=ctype or "неизвестно",
                host=urlparse(final).hostname, resumable=code == 206 or h.get("accept-ranges") == "bytes")


@app.post("/api/info")
async def info(body: UrlIn):
    return await probe(body.url.strip())


@app.post("/api/jobs")
async def add_job(body: JobIn):
    url = body.url.strip()
    p = await probe(url)
    if not p["ok"]:
        raise HTTPException(400, p["warning"])
    title = re.sub(r'[\\/:*?"<>|]+', " ", body.title).strip().lstrip(".")
    if not title:
        raise HTTPException(400, "Введите название фильма")
    jid = uuid.uuid4().hex[:8]
    jobs[jid] = dict(id=jid, url=url, title=title, filename=title + p["ext"], size=p["size"], total=p["size"],
                     done=0, speed=0, eta=None, status="queued", error="", created=time.time())
    await queue.put(jid)
    return public(jobs[jid])


def public(j: dict) -> dict:
    return {k: v for k, v in j.items() if k != "url"}


@app.get("/api/jobs")
async def list_jobs():
    return [public(j) for j in sorted(jobs.values(), key=lambda j: -j["created"])[:30]]


@app.post("/api/jobs/{jid}/cancel")
async def cancel(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "Задача не найдена")
    if j["status"] in ("queued", "running"):
        j["status"] = "cancelled"
        if jid in procs:
            procs[jid].terminate()
    return public(j)


@app.get("/health")
async def health():
    return {"ok": True, "queue": queue.qsize()}


async def worker():
    while True:
        jid = await queue.get()
        j = jobs[jid]
        if j["status"] != "queued":
            continue
        j["status"] = "running"
        cmd = ["rclone", "copyurl", j["url"], f"{REMOTE}/{j['filename']}", "--config", str(CONF),
               "--transfers", "1", "--drive-chunk-size", "16M", "--buffer-size", "8M",
               "--retries", "3", "--low-level-retries", "10", "--user-agent", UA,
               "--stats", "1s", "--use-json-log", "-v"]
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        procs[jid] = p
        async for line in p.stderr:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            s = d.get("stats")
            if s:
                j["done"], j["speed"], j["eta"] = s.get("bytes", 0), s.get("speed", 0), s.get("eta")
                j["total"] = s.get("totalBytes") or j["size"]
            elif d.get("level") in ("error", "critical"):
                j["error"] = str(d.get("msg", ""))[:200]
        rc = await p.wait()
        procs.pop(jid, None)
        if j["status"] == "cancelled":
            continue
        j["status"] = "done" if rc == 0 else "failed"
        if rc == 0:
            j["done"], j["eta"], j["speed"] = j["size"] or j["done"], 0, 0


@app.on_event("startup")
async def startup():
    if os.getenv("rclone"):
        CONF.write_bytes(base64.b64decode(os.environ["rclone"]))
    asyncio.create_task(worker())


app.mount("/", StaticFiles(directory="static", html=True), name="static")
