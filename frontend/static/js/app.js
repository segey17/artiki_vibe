const API = '';
let token = localStorage.getItem('token') || null;
let me = null;
let currentPhotoId = null;
let selectedTier = null;
let tierLabels = {S:'S',A:'A',B:'B',C:'C',D:'D'};
let pollTimer = null;

const TIER_ORDER = ['S','A','B','C','D'];
const TIER_COLORS = {S:'#ff6b6b',A:'#ffa94d',B:'#ffd43b',C:'#74c0fc',D:'#a9e34b'};

document.addEventListener('DOMContentLoaded', async () => {
  if (token) {
    try {
      me = await req('GET', '/api/me');
      await loadTierLabels();
      showVote();
    } catch {
      token = null;
      localStorage.removeItem('token');
      showScreen('auth');
    }
  } else {
    showScreen('auth');
  }
});

// ── SCREENS ──────────────────────────────────────────────────────────────────

function showScreen(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  const el = document.getElementById('screen-' + name);
  if (el) el.classList.add('active');
}

function showVote() {
  showScreen('vote');
  document.getElementById('nav-username').textContent = me?.username || '';
  const adminBtn = document.getElementById('btn-admin-panel');
  const adminBar = document.getElementById('admin-controls');
  if (me?.is_admin) { adminBtn.style.display = ''; adminBar.style.display = ''; }
  else { adminBtn.style.display = 'none'; adminBar.style.display = 'none'; }
  loadCurrentPhoto();
  startPolling();
}

function showAdmin() {
  clearPolling();
  showScreen('admin');
  loadPhotos();
  loadAdminStats();
  renderTierEditor();
}

function showTierlist() {
  clearPolling();
  showScreen('tierlist');
  loadTierlist();
}

// ── AUTH ─────────────────────────────────────────────────────────────────────

function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach((b,i) => b.classList.toggle('active', (i===0)===(tab==='login')));
  document.getElementById('form-login').classList.toggle('hidden', tab !== 'login');
  document.getElementById('form-register').classList.toggle('hidden', tab !== 'register');
}

async function doLogin(e) {
  e.preventDefault();
  try {
    const data = await formReq('/api/login', {
      username: document.getElementById('login-username').value,
      password: document.getElementById('login-password').value,
    });
    token = data.token; localStorage.setItem('token', token);
    me = {username: data.username, is_admin: data.is_admin};
    await loadTierLabels();
    showVote();
  } catch(err) { document.getElementById('login-error').textContent = err.message || 'Ошибка входа'; }
}

async function doRegister(e) {
  e.preventDefault();
  try {
    const data = await formReq('/api/register', {
      username: document.getElementById('reg-username').value,
      password: document.getElementById('reg-password').value,
    });
    token = data.token; localStorage.setItem('token', token);
    me = {username: data.username, is_admin: data.is_admin};
    await loadTierLabels();
    showVote();
  } catch(err) { document.getElementById('reg-error').textContent = err.message || 'Ошибка регистрации'; }
}

function logout() {
  clearPolling(); token = null; me = null;
  localStorage.removeItem('token');
  showScreen('auth');
}

// ── TIER LABELS ───────────────────────────────────────────────────────────────

async function loadTierLabels() {
  try { tierLabels = await req('GET', '/api/tier-labels'); } catch {}
}

function renderTierEditor() {
  const el = document.getElementById('tier-editor');
  if (!el) return;
  el.innerHTML = TIER_ORDER.map(t => `
    <div class="tier-edit-row">
      <div class="tier-edit-badge" style="background:${TIER_COLORS[t]};color:${t==='S'||t==='D'?'#fff':'#1a1000'}">${t}</div>
      <input class="tier-edit-input" id="tier-input-${t}" value="${tierLabels[t]||t}" maxlength="20" placeholder="Название тира ${t}">
    </div>
  `).join('');
}

async function saveTierLabels() {
  const fields = {};
  TIER_ORDER.forEach(t => {
    fields[t.toLowerCase()] = document.getElementById('tier-input-' + t)?.value || t;
  });
  try {
    tierLabels = await formReq('/api/admin/tier-labels', fields);
    toast('Названия сохранены!', 'ok');
    // обновим кнопки голосования если на экране vote
    renderVoteBtns(selectedTier);
  } catch(e) { toast(e.message, 'err'); }
}

// ── CURRENT PHOTO & VOTING ────────────────────────────────────────────────────

async function loadCurrentPhoto() {
  try {
    const data = await req('GET', '/api/current-photo');
    if (data.tier_labels) tierLabels = data.tier_labels;
    renderCurrentPhoto(data);
  } catch(e) { console.error(e); }
}

function renderCurrentPhoto(data) {
  const noPhoto = document.getElementById('no-photo');
  const voteContent = document.getElementById('vote-content');
  const photo = data.photo;

  if (!photo) {
    noPhoto.style.display = '';
    voteContent.style.display = 'none';
    return;
  }
  noPhoto.style.display = 'none';
  voteContent.style.display = '';

  currentPhotoId = photo.id;
  document.getElementById('current-photo-img').src = `/photos/${photo.filename}`;
  document.getElementById('photo-name').textContent = photo.original_name || photo.filename;

  const voteCount = photo.vote_count || 0;
  const total = data.total_users || 1;
  const pct = Math.min(100, Math.round((voteCount / total) * 100));
  document.getElementById('progress-fraction').textContent = `${voteCount} / ${total}`;
  document.getElementById('progress-fill').style.width = pct + '%';

  const voteSection = document.getElementById('vote-stars-section');
  const closedMsg = document.getElementById('vote-closed-msg');
  const votedBadge = document.getElementById('voted-badge');

  if (!data.voting_open) {
    voteSection.style.display = 'none';
    closedMsg.style.display = '';
    votedBadge.style.display = 'none';
  } else if (data.user_tier) {
    selectedTier = data.user_tier;
    voteSection.style.display = '';
    closedMsg.style.display = 'none';
    votedBadge.style.display = '';
    const label = tierLabels[data.user_tier] || data.user_tier;
    document.getElementById('voted-text').textContent = `Вы оценили: ${data.user_tier} — ${label}`;
    renderVoteBtns(data.user_tier, true);
    document.getElementById('btn-vote').disabled = true;
    document.getElementById('btn-vote').textContent = 'Оценка сохранена';
  } else {
    selectedTier = null;
    voteSection.style.display = '';
    closedMsg.style.display = 'none';
    votedBadge.style.display = 'none';
    renderVoteBtns(null, false);
    document.getElementById('btn-vote').disabled = true;
    document.getElementById('btn-vote').textContent = 'Выберите оценку';
  }

  if (me?.is_admin) {
    document.getElementById('admin-stats').textContent =
      `Голосов: ${voteCount} | Средняя: ${photo.avg_score || '—'}`;
  }
}

function renderVoteBtns(activeTier, locked = false) {
  const container = document.getElementById('tier-vote-btns');
  container.innerHTML = TIER_ORDER.map(t => {
    const label = tierLabels[t] || t;
    const isActive = t === activeTier;
    return `<button 
      class="tier-vote-btn ${isActive ? 'active' : ''}" 
      data-tier="${t}"
      style="--tc:${TIER_COLORS[t]}"
      onclick="${locked ? '' : `selectTier('${t}')`}"
      ${locked ? 'disabled' : ''}
    >
      <span class="tvb-letter">${t}</span>
      <span class="tvb-label">${label}</span>
    </button>`;
  }).join('');
}

function selectTier(t) {
  selectedTier = t;
  renderVoteBtns(t, false);
  const btn = document.getElementById('btn-vote');
  btn.disabled = false;
  btn.textContent = `Поставить ${t} — ${tierLabels[t] || t}`;
}

async function submitVote() {
  if (!selectedTier || !currentPhotoId) return;
  try {
    await formReq('/api/rate', {photo_id: currentPhotoId, tier: selectedTier});
    toast('Оценка сохранена!', 'ok');
    loadCurrentPhoto();
  } catch(e) { toast(e.message || 'Ошибка', 'err'); }
}

// ── POLLING ───────────────────────────────────────────────────────────────────

function startPolling() { clearPolling(); pollTimer = setInterval(loadCurrentPhoto, 5000); }
function clearPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

// ── ADMIN NAV ─────────────────────────────────────────────────────────────────

async function nextPhoto() {
  try {
    const data = await req('POST', '/api/admin/next-photo');
    if (data.done) toast('Все фотографии просмотрены! Смотрите тир-лист.', 'ok');
    else { selectedTier = null; loadCurrentPhoto(); }
  } catch(e) { toast(e.message, 'err'); }
}

async function prevPhoto() {
  try { await req('POST', '/api/admin/prev-photo'); selectedTier = null; loadCurrentPhoto(); }
  catch(e) { toast(e.message || 'Уже первая фотография', 'err'); }
}

async function closeVoting() {
  try { await req('POST', '/api/admin/close-voting'); toast('Голосование закрыто', 'ok'); loadCurrentPhoto(); }
  catch(e) { toast(e.message, 'err'); }
}

// ── ADMIN STATS ───────────────────────────────────────────────────────────────

async function loadAdminStats() {
  try {
    const d = await req('GET', '/api/stats');
    const el = document.getElementById('admin-stats-card');
    if (!el) return;
    el.innerHTML = [['Фотографий',d.total_photos],['Оценено',d.rated_photos],
      ['Пользователей',d.total_users],['Всего голосов',d.total_votes]]
      .map(([l,n]) => `<div class="stat-box"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join('');
  } catch {}
}

// ── ADMIN PHOTOS ──────────────────────────────────────────────────────────────

async function loadPhotos() {
  try {
    const photos = await req('GET', '/api/admin/photos');
    document.getElementById('photo-count-label').textContent = `(${photos.length})`;
    document.getElementById('photos-tbody').innerHTML = photos.map((p,i) => `
      <tr>
        <td>${i+1}</td>
        <td><img src="/photos/${p.filename}" alt=""></td>
        <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${p.original_name}">${p.original_name||p.filename}</td>
        <td>${p.vote_count}</td>
        <td>${p.avg_score??'—'}</td>
        <td>
          <button class="set-btn" onclick="setPhoto(${p.id})">▶</button>
          <button class="del-btn" onclick="deletePhoto(${p.id})">✕</button>
        </td>
      </tr>`).join('');
  } catch(e) { toast(e.message,'err'); }
}

async function setPhoto(id) {
  await req('POST', `/api/admin/set-photo/${id}`);
  toast('Фотография поставлена','ok');
}

async function deletePhoto(id) {
  if (!confirm('Удалить фотографию и все её оценки?')) return;
  try { await req('DELETE', `/api/admin/photos/${id}`); toast('Удалено','ok'); loadPhotos(); }
  catch(e) { toast(e.message,'err'); }
}

// ── UPLOAD ────────────────────────────────────────────────────────────────────

async function uploadFiles(files) {
  if (!files.length) return;
  const BATCH = 20;
  let done = 0, total = files.length;
  document.getElementById('upload-progress').style.display = '';
  setUpProgress(0, `Загрузка 0 / ${total}...`);
  for (let i = 0; i < total; i += BATCH) {
    const fd = new FormData();
    Array.from(files).slice(i, i+BATCH).forEach(f => fd.append('files', f));
    try {
      const resp = await fetch(API+'/api/admin/photos/upload', {
        method:'POST', headers:{'Authorization':'Bearer '+token}, body:fd
      });
      if (!resp.ok) throw new Error(await resp.text());
      done += (await resp.json()).added;
    } catch(e) { toast('Ошибка батча: '+e.message,'err'); }
    setUpProgress(Math.round(((i+BATCH)/total)*100), `Загружено ${Math.min(done,total)} / ${total}`);
  }
  setUpProgress(100, `Готово! Добавлено ${done} фото.`);
  setTimeout(() => { document.getElementById('upload-progress').style.display='none'; }, 3000);
  loadPhotos();
}

function handleDrop(e) { e.preventDefault(); uploadFiles(e.dataTransfer.files); }
function setUpProgress(pct, label) {
  document.getElementById('up-fill').style.width = pct+'%';
  document.getElementById('up-label').textContent = label;
}

// ── TIERLIST ──────────────────────────────────────────────────────────────────

async function loadTierlist() {
  try {
    const data = await req('GET', '/api/tierlist');
    const labels = data.tier_labels || tierLabels;
    const container = document.getElementById('tierlist-container');
    const empty = document.getElementById('tierlist-empty');
    let hasAny = false;
    container.innerHTML = data.tier_order.map(tier => {
      const photos = data.tiers[tier] || [];
      if (photos.length) hasAny = true;
      const label = labels[tier] || tier;
      const color = TIER_COLORS[tier];
      const textColor = tier==='S'||tier==='D' ? '#fff' : '#1a1000';
      return `
        <div class="tier-row tier-${tier}">
          <div class="tier-label" style="background:${color};color:${textColor}">
            <span class="tier-letter">${tier}</span>
            <span class="tier-name">${label !== tier ? label : ''}</span>
          </div>
          <div class="tier-photos">
            ${photos.length
              ? photos.map(p => `
                  <div class="tier-photo" title="${p.original_name||p.filename} — ${p.avg_score}★ (${p.vote_count} гол.)">
                    <img src="/photos/${p.filename}" alt="" loading="lazy">
                    <div class="score-badge">${p.avg_score}</div>
                  </div>`).join('')
              : '<span class="tier-empty">—</span>'}
          </div>
        </div>`;
    }).join('');
    empty.style.display = hasAny ? 'none' : '';
  } catch(e) { toast(e.message,'err'); }
}

// ── HTTP ──────────────────────────────────────────────────────────────────────

async function req(method, url, body) {
  const opts = {method, headers:{'Authorization':'Bearer '+token}};
  if (body) { opts.headers['Content-Type']='application/json'; opts.body=JSON.stringify(body); }
  const resp = await fetch(API+url, opts);
  const text = await resp.text();
  let json; try { json=JSON.parse(text); } catch { json=text; }
  if (!resp.ok) throw new Error(json?.detail||json||resp.statusText);
  return json;
}

async function formReq(url, fields) {
  const fd = new FormData();
  Object.entries(fields).forEach(([k,v]) => fd.append(k, v));
  const resp = await fetch(API+url, {
    method:'POST',
    headers: token ? {'Authorization':'Bearer '+token} : {},
    body: fd,
  });
  const text = await resp.text();
  let json; try { json=JSON.parse(text); } catch { json=text; }
  if (!resp.ok) throw new Error(json?.detail||json||resp.statusText);
  return json;
}

let toastTimer;
function toast(msg, type='') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (type ? ' '+type : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
}
