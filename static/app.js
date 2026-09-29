const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const fmt = bytes => {
  if (!bytes) return '0 Б';
  const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
  const index = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)));
  return (bytes / 1024 ** index).toFixed(index ? 1 : 0) + ' ' + units[index];
};
const fmtTime = seconds => {
  if (seconds == null) return '—';
  seconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return (hours ? hours + 'ч ' : '') + (minutes ? minutes + 'м ' : '') + (seconds % 60) + 'с';
};
const labels = { queued: 'В ОЧЕРЕДИ', running: 'ЗАГРУЗКА', done: 'ГОТОВО', failed: 'ОШИБКА', cancelled: 'ОТМЕНЕНО' };
let lastInfo = null;

function msg(text, type = '') {
  $('msg').className = 'status ' + type;
  $('msg').innerHTML = `<span class="status-dot"></span><span>${esc(text)}</span>`;
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Ошибка ' + response.status);
  return data;
}

function reset() {
  $('step2').hidden = true;
  $('url').value = '';
  $('title').value = '';
  lastInfo = null;
}

function modeLabel(method) {
  return method === 'yt-dlp' ? 'SMART / YT-DLP' : 'DIRECT / RCLONE';
}

function makeCurl() {
  const url = $('url').value.trim() || 'https://site.example/watch/film';
  const title = $('title').value.trim() || 'Мой фильм';
  const mode = $('mode').value;
  return `curl.exe -G "${location.origin}/api/download" ` +
    `\`\n  --data-urlencode "url=${url}" \`\n  --data-urlencode "title=${title}" \`\n  --data-urlencode "mode=${mode}"`;
}

function updateCurl() {
  $('curlExample').textContent = makeCurl();
}

$('check').onclick = async () => {
  const url = $('url').value.trim();
  if (!url) return msg('Вставьте ссылку.', 'err');
  $('check').disabled = true;
  msg('Сканирую ссылку и ищу видеопоток…');
  try {
    const data = await api('/api/info', { url, mode: $('mode').value });
    lastInfo = data;
    const rows = [
      ['Источник', data.host || '—'],
      ['Название', data.name || data.title || '—'],
      ['Размер', data.size ? fmt(data.size) : 'неизвестен'],
      ['Тип', data.type || '—'],
      ['Способ', modeLabel(data.method)],
      ['Докачка', data.resumable ? 'поддерживается' : 'неизвестно'],
    ];
    $('info').innerHTML = rows.map(row => `<tr><td>${esc(row[0])}</td><td>${esc(row[1])}</td></tr>`).join('');
    $('title').value = $('title').value || data.title || (data.name || '').replace(/\.[^.]+$/, '');
    $('methodChip').textContent = modeLabel(data.method);
    $('methodChip').className = 'chip accent ' + (data.method === 'yt-dlp' ? 'smart' : 'direct');
    $('step2').hidden = false;
    $('confirm').disabled = !data.ok;
    data.ok ? msg(data.warning || 'Источник найден. Проверьте имя и добавьте его в очередь.', 'ok') : msg(data.warning || 'Источник не подходит.', 'err');
    updateCurl();
  } catch (error) {
    msg('Ошибка: ' + error.message, 'err');
  } finally {
    $('check').disabled = false;
  }
};

$('reject').onclick = () => { reset(); msg('Сброшено. Ничего не загружено.'); };

$('confirm').onclick = async () => {
  const title = $('title').value.trim();
  if (!title) return msg('Введите имя файла.', 'err');
  if (!lastInfo) return msg('Сначала просканируйте ссылку.', 'err');
  $('confirm').disabled = true;
  try {
    const job = await api('/api/jobs', { url: $('url').value.trim(), title, mode: $('mode').value });
    reset();
    msg(`Задача ${job.id} добавлена в очередь.`, 'ok');
    refresh();
  } catch (error) {
    msg('Ошибка: ' + error.message, 'err');
  } finally {
    $('confirm').disabled = false;
  }
};

$('jobs').onclick = async event => {
  const button = event.target.closest('[data-cancel]');
  if (!button) return;
  button.disabled = true;
  try { await api(`/api/jobs/${button.dataset.cancel}/cancel`, {}); refresh(); }
  catch (error) { msg('Ошибка отмены: ' + error.message, 'err'); }
};

$('copyCurl').onclick = async () => {
  updateCurl();
  try {
    await navigator.clipboard.writeText($('curlExample').textContent);
    msg('Команда curl скопирована.', 'ok');
  } catch { msg('Не удалось скопировать команду.', 'err'); }
};

async function refresh() {
  try {
    const list = await api('/api/jobs');
    $('queueCount').textContent = `${list.length} TASK${list.length === 1 ? '' : 'S'}`;
    if (!list.length) {
      $('jobs').innerHTML = '<div class="empty"><span>◌</span> Очередь пока пуста</div>';
      return;
    }
    $('jobs').innerHTML = list.map(job => {
      const total = job.total || job.size || 0;
      const percent = job.status === 'done' ? 100 : total ? Math.min(100, job.done / total * 100) : 0;
      const active = job.status === 'queued' || job.status === 'running';
      const detail = job.status === 'running'
        ? `${fmt(job.done)} / ${total ? fmt(total) : '?'} · ${fmt(job.speed)}/с · осталось ${fmtTime(job.eta)}`
        : job.status === 'failed' ? (job.error || 'неизвестная ошибка')
          : job.status === 'done' ? fmt(job.size || job.done) : job.stage || '';
      return `<article class="job">
        <div class="job-top"><div><span class="job-name">${esc(job.filename)}</span><span class="job-id">#${esc(job.id)}</span></div>
          <span class="job-method">${esc(job.method === 'yt-dlp' ? 'YT-DLP' : 'RCLONE')}</span></div>
        <div class="bar"><i style="width:${percent}%"></i></div>
        <div class="job-bottom"><span class="job-status ${esc(job.status)}">${labels[job.status] || job.status}</span><span class="percent">${percent.toFixed(0)}%</span><span class="job-detail">${esc(job.stage || detail)}${job.status === 'running' ? ' · ' + esc(detail) : ''}</span>
          ${active ? `<button class="btn tiny cancel" data-cancel="${esc(job.id)}">ОТМЕНА</button>` : ''}</div>
      </article>`;
    }).join('');
  } catch { /* Render может просыпаться — следующий интервал повторит запрос. */ }
}

$('url').addEventListener('input', updateCurl);
$('title').addEventListener('input', updateCurl);
$('mode').addEventListener('change', updateCurl);
setInterval(refresh, 2000);
refresh();
updateCurl();
setInterval(() => { $('clock').textContent = new Date().toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' }); }, 1000);
