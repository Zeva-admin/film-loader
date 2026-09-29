const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = b => { if (!b) return '0 Б'; const u = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ']; const i = Math.min(4, Math.floor(Math.log(b) / Math.log(1024))); return (b / 1024 ** i).toFixed(i ? 1 : 0) + ' ' + u[i]; };
const fmtT = s => { if (s == null) return '—'; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return (h ? h + 'ч ' : '') + (m ? m + 'м ' : '') + (s % 60) + 'с'; };
const LABEL = { queued: 'В очереди', running: 'Загрузка', done: 'Готово ✔', failed: 'Ошибка ✖', cancelled: 'Отменено' };

function msg(text, cls = '') { $('msg').textContent = text; $('msg').className = 'status ' + cls; }

async function api(path, body) {
  const r = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || 'Ошибка ' + r.status);
  return d;
}

function reset() { $('step2').hidden = true; $('url').value = ''; $('title').value = ''; }

$('check').onclick = async () => {
  const url = $('url').value.trim();
  if (!url) return msg('Вставьте ссылку.', 'err');
  msg('Проверяю ссылку… (сервер может просыпаться до минуты)');
  try {
    const d = await api('/api/info', { url });
    const rows = [['Имя в ссылке', d.name], ['Расширение', d.ext || 'не определено'], ['Размер', d.size ? fmt(d.size) : 'неизвестно'],
      ['Тип', d.type], ['Сервер', d.host], ['Докачка', d.resumable ? 'поддерживается' : 'нет']];
    $('info').innerHTML = rows.map(r => `<tr><td>${r[0]}:</td><td>${esc(r[1])}</td></tr>`).join('');
    $('title').value = $('title').value || d.name.replace(/\.[^.]+$/, '');
    $('step2').hidden = false;
    $('confirm').disabled = !d.ok;
    d.ok ? msg(d.warning || 'Ссылка в порядке. Введите название и подтвердите.', 'ok') : msg(d.warning, 'err');
  } catch (e) { msg('Ошибка: ' + e.message, 'err'); }
};

$('reject').onclick = () => { reset(); msg('Отказ: ничего не загружено.'); };

$('confirm').onclick = async () => {
  const title = $('title').value.trim();
  if (!title) return msg('Введите название фильма.', 'err');
  try {
    const j = await api('/api/jobs', { url: $('url').value.trim(), title });
    reset(); msg(`Статус: ОК. «${j.filename}» добавлен в очередь.`, 'ok'); refresh();
  } catch (e) { msg('Ошибка: ' + e.message, 'err'); }
};

$('jobs').onclick = async e => {
  const id = e.target.dataset.cancel;
  if (id) { await api(`/api/jobs/${id}/cancel`, {}); refresh(); }
};

async function refresh() {
  try {
    const list = await api('/api/jobs');
    if (!list.length) return;
    $('jobs').innerHTML = list.map(j => {
      const total = j.total || j.size || 0;
      const p = j.status === 'done' ? 100 : total ? Math.min(100, j.done / total * 100) : 0;
      const active = j.status === 'queued' || j.status === 'running';
      const detail = j.status === 'running' ? `${fmt(j.done)} / ${total ? fmt(total) : '?'} · ${fmt(j.speed)}/с · осталось ${fmtT(j.eta)}`
        : j.status === 'failed' ? esc(j.error || 'неизвестная ошибка') : j.status === 'done' ? fmt(j.size || j.done) : '';
      return `<div class="job"><div class="top"><span class="name">${esc(j.filename)}</span>
        ${active ? `<button class="btn" data-cancel="${j.id}">Отмена</button>` : ''}</div>
        <div class="bar"><i style="width:${p}%"></i></div><small>${LABEL[j.status]} ${p.toFixed(0)}% ${detail}</small></div>`;
    }).join('');
  } catch (e) { /* сервер просыпается — пробуем снова */ }
}

setInterval(refresh, 2000); refresh();
setInterval(() => $('clock').textContent = new Date().toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' }), 1000);
