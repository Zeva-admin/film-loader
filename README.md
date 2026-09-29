# Film Loader

Ретро-техно интерфейс для загрузки прямых файлов и видео со страниц на Google Drive.

## Запуск

Проект рассчитан на Python 3.11. В контейнере автоматически устанавливаются `yt-dlp`, `ffmpeg` и `rclone`.

Переменные окружения:

- `REMOTE` — remote и папка rclone, по умолчанию `gdrive_model3m:films`;
- `MAX_GB` — максимальный размер файла, по умолчанию `20`;
- `RCLONE_CONFIG_B64` или старое имя `rclone` — base64-конфигурация rclone;
- `PORT` — порт HTTP-сервера, по умолчанию `10000`.

## API из curl

Удобная PowerShell-команда:

```powershell
curl.exe -G "https://film-loader-cacut.onrender.com/api/download" `
  --data-urlencode "url=https://site.example/watch/film" `
  --data-urlencode "title=Мой фильм" `
  --data-urlencode "mode=auto"
```

Короткая форма для прямой ссылки:

```powershell
curl.exe "https://film-loader-cacut.onrender.com/api/https://site.example/video.mp4"
```

Для страниц с плеером используйте `mode=smart`. В режиме `auto` сервис сначала проверяет прямой файл, а затем передаёт страницу `yt-dlp`. Ответом будет JSON с `id` задачи и её текущим статусом.

Проверить очередь:

```powershell
curl.exe "https://film-loader-cacut.onrender.com/api/jobs"
curl.exe "https://film-loader-cacut.onrender.com/api/jobs/JOB_ID"
```

Отменить задачу:

```powershell
curl.exe -X POST "https://film-loader-cacut.onrender.com/api/jobs/JOB_ID/cancel"
```

Также доступны интерактивная документация FastAPI по `/docs` и проверка состояния по `/health`.
