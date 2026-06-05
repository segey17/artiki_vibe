const API = '';
let token = localStorage.getItem('token') || null;
let me = null;
let currentPhotoId = null;
let selectedTierId = null;
let tiers = [];
let pollTimer = null;
let ws = null;
let wsReconnectTimer = null;
let autoAdvanceEnabled = false;

document.addEventListener('DOMContentLoaded', async () => {
  if (token) {
    try {
      me = await req('GET', '/api/me');
      tiers = await req('GET', '/api/tiers');
      showVote();
    } catch { token = null; localStorage.removeItem('token'); showScreen('auth'); }
  } else { showScreen('auth'); }
});

// ── SCREENS ──────────────────────────────────────────────────────────────────
function showScreen(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById('screen-' + name)?.classList.add('active');
}
function showVote() {
  showScreen('vote');
  document.getElementById('nav-username').textContent = me?.username || '';
  document.getElementById('btn-admin-panel').style.display = me?.is_admin ? '' : 'none';
  document.getElementById('admin-controls').style.display = me?.is_admin ? '' : 'none';
  loadCurrentPhoto(); startPolling();
  if (me?.is_admin) loadAutoAdvanceState();
}
function showAdmin() { clearPolling(); showScreen('admin'); loadPhotos(); loadAdminStats(); renderTierEditor(); loadWatchStatus(); }

// ── AUTH ─────────────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach((b,i) => b.classList.toggle('active',(i===0)===(tab==='login')));
  document.getElementById('form-login').classList.toggle('hidden', tab!=='login');
  document.getElementById('form-register').classList.toggle('hidden', tab!=='register');
}
async function doLogin(e) {
  e.preventDefault();
  try {
    const d = await formReq('/api/login', {
      username: document.getElementById('login-username').value,
      password: document.getElementById('login-password').value });
    token = d.token; localStorage.setItem('token', token);
    me = {username: d.username, is_admin: d.is_admin};
    tiers = await req('GET', '/api/tiers');
    showVote();
  } catch(e) { document.getElementById('login-error').textContent = e.message; }
}
async function doRegister(e) {
  e.preventDefault();
  try {
    const d = await formReq('/api/register', {
      username: document.getElementById('reg-username').value,
      password: document.getElementById('reg-password').value });
    token = d.token; localStorage.setItem('token', token);
    me = {username: d.username, is_admin: d.is_admin};
    tiers = await req('GET', '/api/tiers');
    showVote();
  } catch(e) { document.getElementById('reg-error').textContent = e.message; }
}
function logout() {
  clearPolling(); token = null; me = null; localStorage.removeItem('token'); showScreen('auth');
}

// ── VOTING ────────────────────────────────────────────────────────────────────
async function loadCurrentPhoto() {
  try {
    const data = await req('GET', '/api/current-photo');
    if (data.tiers?.length) tiers = data.tiers;
    renderCurrentPhoto(data);
    loadTags();
  } catch(e) { console.error(e); }
}

function renderCurrentPhoto(data) {
  const photo = data.photo;
  document.getElementById('no-photo').style.display = photo ? 'none' : '';
  document.getElementById('vote-content').style.display = photo ? '' : 'none';
  if (!photo) return;

  // reset votes list on photo change
  if (currentPhotoId !== photo.id) {
    votesListOpen = false;
    const list = document.getElementById('votes-list');
    const label = document.getElementById('votes-toggle-label');
    if (list) list.style.display = 'none';
    if (label) label.textContent = 'Показать голоса ▾';
  }
  currentPhotoId = photo.id;
  document.getElementById('current-photo-img').src = `/photos/${photo.filename}`;
  document.getElementById('photo-name').textContent = photo.original_name || photo.filename;

  const voteCount = photo.vote_count || 0;
  const total = data.total_users || 1;
  document.getElementById('progress-fraction').textContent = `${voteCount} / ${total}`;
  document.getElementById('progress-fill').style.width = Math.min(100, Math.round(voteCount/total*100)) + '%';

  const voteSection = document.getElementById('vote-stars-section');
  const closedMsg  = document.getElementById('vote-closed-msg');
  const votedBadge = document.getElementById('voted-badge');
  const tierCounts = data.tier_counts || {};

  if (!data.voting_open) {
    voteSection.style.display = 'none'; closedMsg.style.display = ''; votedBadge.style.display = 'none';
  } else if (data.user_tier) {
    selectedTierId = data.user_tier;
    voteSection.style.display = ''; closedMsg.style.display = 'none'; votedBadge.style.display = '';
    const t = tiers.find(t => t.id === data.user_tier);
    document.getElementById('voted-text').textContent = `Ваша оценка: ${t?.label || data.user_tier}`;
    renderVoteBtns(data.user_tier, true, tierCounts);
    document.getElementById('btn-vote').disabled = true;
    document.getElementById('btn-vote').textContent = 'Оценка сохранена';
  } else {
    selectedTierId = null;
    voteSection.style.display = ''; closedMsg.style.display = 'none'; votedBadge.style.display = 'none';
    renderVoteBtns(null, false, tierCounts);
    document.getElementById('btn-vote').disabled = true;
    document.getElementById('btn-vote').textContent = 'Выберите оценку';
  }
  if (me?.is_admin)
    document.getElementById('admin-stats').textContent = `Голосов: ${voteCount}`;
}

function renderVoteBtns(activeId, locked, counts = {}) {
  document.getElementById('tier-vote-btns').innerHTML = tiers.map(t => {
    const cnt = counts[t.id] || 0;
    const isActive = t.id === activeId;
    return `<button class="tier-vote-btn ${isActive?'active':''}"
      data-id="${t.id}"
      style="--tc:${t.color}"
      onclick="${locked ? '' : `selectTier('${t.id}')`}"
      ${locked ? 'disabled' : ''}>
      <span class="tvb-swatch"></span>
      <span class="tvb-label">${t.label}</span>
      ${cnt ? `<span class="tvb-count">${cnt}</span>` : ''}
    </button>`;
  }).join('');
}

function selectTier(id) {
  selectedTierId = id;
  renderVoteBtns(id, false);
  const t = tiers.find(t => t.id === id);
  const btn = document.getElementById('btn-vote');
  btn.disabled = false;
  btn.textContent = `Поставить: ${t?.label || id}`;
}

async function submitVote() {
  if (!selectedTierId || !currentPhotoId) return;
  try {
    await formReq('/api/rate', {photo_id: currentPhotoId, tier_id: selectedTierId});
    toast('Оценка сохранена!', 'ok'); loadCurrentPhoto();
  } catch(e) { toast(e.message || 'Ошибка', 'err'); }
}

function startPolling() {
  clearPolling();
  // fallback polling (в случае если WS недоступен)
  pollTimer = setInterval(loadCurrentPhoto, 15000);
  connectWS();
}

function clearPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
}

function connectWS() {
  if (!token) return;
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);

  ws.onopen = () => {
    console.log('WS connected');
  };

  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'photo_change' || msg.type === 'voting_closed' || msg.type === 'vote_update') {
        selectedTierId = null;
        loadCurrentPhoto();
      }
      if (msg.type === 'vote_update' || msg.type === 'photo_change') {
        if (me?.is_admin) loadOnlineUsers();
      }
      if (msg.type === 'auto_advance_changed') {
        autoAdvanceEnabled = msg.enabled;
        updateAutoAdvanceBtn();
        if (msg.enabled) loadOnlineUsers();
        else document.getElementById('online-users-bar').style.display = 'none';
      }
      if (msg.type === 'all_done') {
        selectedTierId = null;
        loadCurrentPhoto();
        if (me?.is_admin) toast('Все фото просмотрены!', 'ok');
      }
    } catch {}
  };

  ws.onclose = () => {
    console.log('WS disconnected, reconnecting in 3s...');
    wsReconnectTimer = setTimeout(connectWS, 3000);
  };

  ws.onerror = () => { ws.close(); };
}

// ── ADMIN NAV ─────────────────────────────────────────────────────────────────
async function nextPhoto() {
  try {
    const d = await req('POST', '/api/admin/next-photo');
    if (d.done) toast('Все фотографии просмотрены!', 'ok');
    else { selectedTierId = null; loadCurrentPhoto(); }
  } catch(e) { toast(e.message,'err'); }
}
async function prevPhoto() {
  try { await req('POST', '/api/admin/prev-photo'); selectedTierId=null; loadCurrentPhoto(); }
  catch(e) { toast(e.message||'Уже первая','err'); }
}
async function closeVoting() {
  try { await req('POST','/api/admin/close-voting'); toast('Закрыто','ok'); loadCurrentPhoto(); }
  catch(e) { toast(e.message,'err'); }
}

async function shufflePhotos() {
  if (!confirm('Перемешать все фотографии в случайном порядке и начать с первой?')) return;
  try {
    const d = await req('POST', '/api/admin/shuffle');
    toast(`Перемешано ${d.count} фото 🔀`, 'ok');
    selectedTierId = null;
    loadCurrentPhoto();
  } catch(e) { toast(e.message, 'err'); }
}

async function toggleAutoAdvance() {
  try {
    const newState = !autoAdvanceEnabled;
    await formReq('/api/admin/auto-advance', { enabled: newState });
    autoAdvanceEnabled = newState;
    updateAutoAdvanceBtn();
    toast(newState ? '⚡ Авто-переход включён' : 'Авто-переход выключен', 'ok');
    if (newState) loadOnlineUsers();
    else document.getElementById('online-users-bar').style.display = 'none';
  } catch(e) { toast(e.message, 'err'); }
}

function updateAutoAdvanceBtn() {
  const btn = document.getElementById('auto-advance-btn');
  if (!btn) return;
  if (autoAdvanceEnabled) {
    btn.classList.add('active');
    btn.title = 'Авто-переход ВКЛЮЧЁН — выключить';
  } else {
    btn.classList.remove('active');
    btn.title = 'Авто-переход выключен — включить';
  }
}

async function loadAutoAdvanceState() {
  if (!me?.is_admin) return;
  try {
    const d = await req('GET', '/api/admin/auto-advance');
    autoAdvanceEnabled = d.enabled;
    updateAutoAdvanceBtn();
    if (d.enabled) loadOnlineUsers();
  } catch {}
}

async function loadOnlineUsers() {
  if (!me?.is_admin) return;
  const bar = document.getElementById('online-users-bar');
  if (!autoAdvanceEnabled) { bar.style.display = 'none'; return; }
  try {
    const d = await req('GET', '/api/online-users');
    if (!d.count) {
      bar.innerHTML = '<span class="online-label">Онлайн: нет участников</span>';
    } else {
      bar.innerHTML = `<span class="online-label">Онлайн (${d.count}):</span> ` +
        d.users.map(u => `<span class="online-user">${u.username}</span>`).join('');
    }
    bar.style.display = '';
  } catch {}
}

// ── HOTKEYS ───────────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  // не срабатывает в полях ввода
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  // не срабатывает если открыто модальное окно
  if (document.getElementById('photo-modal')?.style.display !== 'none') return;
  // только на экране голосования
  if (!document.getElementById('screen-vote')?.classList.contains('active')) return;

  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
    if (me?.is_admin) { e.preventDefault(); nextPhoto(); }
  } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
    if (me?.is_admin) { e.preventDefault(); prevPhoto(); }
  } else if (e.key === ' ' || e.key === 'Enter') {
    // пробел/Enter — подтвердить оценку
    const btn = document.getElementById('btn-vote');
    if (btn && !btn.disabled) { e.preventDefault(); submitVote(); }
  }
});

// ── TIER EDITOR ───────────────────────────────────────────────────────────────
const PALETTE = ['#ff6b6b','#ff8c42','#ffd43b','#a9e34b','#4ae8a0','#74c0fc','#a78bfa','#f472b6','#94a3b8','#ffffff'];

function renderTierEditor() {
  const el = document.getElementById('tier-editor');
  el.innerHTML = tiers.map((t, i) => tierRow(t, i)).join('');
}

function tierRow(t, i) {
  return `<div class="tier-edit-row" data-idx="${i}">
    <div class="tier-color-pick">
      <div class="tier-color-swatch" style="background:${t.color}" onclick="togglePalette(${i})"></div>
      <div class="tier-palette" id="palette-${i}" style="display:none">
        ${PALETTE.map(c => `<div class="pal-dot" style="background:${c}" onclick="pickColor(${i},'${c}')"></div>`).join('')}
        <input type="color" value="${t.color}" oninput="pickColor(${i},this.value)" title="Свой цвет">
      </div>
    </div>
    <input class="tier-edit-input" value="${t.label}" maxlength="30"
      oninput="updateTierLabel(${i},this.value)" placeholder="Название тира">
    <button class="tier-del-btn" onclick="removeTier(${i})" title="Удалить" ${tiers.length<=2?'disabled':''}>✕</button>
    <div class="tier-drag-hint">⠿</div>
  </div>`;
}

function togglePalette(i) {
  document.querySelectorAll('.tier-palette').forEach((p,j) => {
    p.style.display = (j===i && p.style.display==='none') ? 'flex' : 'none';
  });
}

function pickColor(i, color) {
  tiers[i].color = color;
  document.querySelectorAll('.tier-palette')[i].style.display = 'none';
  renderTierEditor();
}

function updateTierLabel(i, val) { tiers[i].label = val; }

function removeTier(i) {
  if (tiers.length <= 2) return;
  tiers.splice(i, 1);
  renderTierEditor();
}

function addTier() {
  if (tiers.length >= 10) { toast('Максимум 10 тиров','err'); return; }
  tiers.push({id: 'new_'+Date.now(), label: 'Новый тир', color: '#888888', order: tiers.length});
  renderTierEditor();
  // scroll to bottom of editor
  document.getElementById('tier-editor').lastElementChild?.scrollIntoView({behavior:'smooth'});
}

async function saveTierLabels() {
  // read current input values
  document.querySelectorAll('.tier-edit-row').forEach((row, i) => {
    const input = row.querySelector('.tier-edit-input');
    if (input) tiers[i].label = input.value.trim() || tiers[i].label;
  });
  try {
    tiers = await req('POST', '/api/admin/tiers', {tiers});
    toast('Тиры сохранены!', 'ok');
    renderTierEditor();
  } catch(e) { toast(e.message,'err'); }
}

// ── ADMIN PHOTOS ──────────────────────────────────────────────────────────────
async function loadAdminStats() {
  try {
    const d = await req('GET','/api/stats');
    const el = document.getElementById('admin-stats-card'); if(!el) return;
    el.innerHTML = [['Фотографий',d.total_photos],['Оценено',d.rated_photos],
      ['Пользователей',d.total_users],['Всего голосов',d.total_votes]]
      .map(([l,n])=>`<div class="stat-box"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join('');
  } catch {}
}

async function loadPhotos() {
  try {
    const photos = await req('GET','/api/admin/photos');
    document.getElementById('photo-count-label').textContent = `(${photos.length})`;
    document.getElementById('photos-tbody').innerHTML = photos.map((p,i)=>`
      <tr>
        <td>${i+1}</td><td><img src="/photos/${p.filename}" alt=""></td>
        <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.original_name||p.filename}</td>
        <td>${p.vote_count}</td>
        <td><button class="set-btn" onclick="setPhoto(${p.id})">▶</button>
            <button class="del-btn" onclick="deletePhoto(${p.id})">✕</button></td>
      </tr>`).join('');
  } catch(e) { toast(e.message,'err'); }
}
async function setPhoto(id) { await req('POST',`/api/admin/set-photo/${id}`); toast('Поставлено','ok'); }
async function deletePhoto(id) {
  if (!confirm('Удалить?')) return;
  try { await req('DELETE',`/api/admin/photos/${id}`); toast('Удалено','ok'); loadPhotos(); }
  catch(e) { toast(e.message,'err'); }
}

async function resetDB() {
  if (!confirm('Вы уверены? Это удалит всех пользователей (кроме администраторов), все фотографии и оценки. Действие необратимо!')) return;
  if (!confirm('Последнее предупреждение: восстановить данные будет невозможно. Продолжить?')) return;
  try {
    await req('POST', '/api/admin/reset-db');
    toast('База данных очищена', 'ok');
    loadPhotos();
    loadAdminStats();
  } catch(e) { toast(e.message || 'Ошибка', 'err'); }
}

async function uploadFiles(files) {
  if (!files.length) return;
  const BATCH=20; let done=0, total=files.length;
  document.getElementById('upload-progress').style.display='';
  setUpProgress(0,`Загрузка 0 / ${total}...`);
  for (let i=0;i<total;i+=BATCH) {
    const fd=new FormData();
    Array.from(files).slice(i,i+BATCH).forEach(f=>fd.append('files',f));
    try {
      const r=await fetch(API+'/api/admin/photos/upload',{method:'POST',headers:{'Authorization':'Bearer '+token},body:fd});
      if(!r.ok) throw new Error(await r.text());
      done+=(await r.json()).added;
    } catch(e) { toast('Ошибка: '+e.message,'err'); }
    setUpProgress(Math.round(((i+BATCH)/total)*100),`Загружено ${Math.min(done,total)} / ${total}`);
  }
  setUpProgress(100,`Готово! Добавлено ${done} фото.`);
  setTimeout(()=>{document.getElementById('upload-progress').style.display='none';},3000);
  loadPhotos();
}
function handleDrop(e){e.preventDefault();uploadFiles(e.dataTransfer.files);}
function setUpProgress(p,l){document.getElementById('up-fill').style.width=p+'%';document.getElementById('up-label').textContent=l;}

// ── TIERLIST ──────────────────────────────────────────────────────────────────
// Cache tierlist data for export
let tierlistData = null;
let allTierlistTags = [];      // [{id, name}] — все теги оценённых фото
let activeTierTagIds = new Set(); // выбранные id тегов для фильтра (включить)
let excludedTierTagIds = new Set(); // исключённые id тегов
let filteredTagSearch = '';    // поисковый запрос в панели тегов

function textColorFor(hex) {
  const h = hex.replace('#','');
  const r=parseInt(h.slice(0,2),16), g=parseInt(h.slice(2,4),16), b=parseInt(h.slice(4,6),16);
  return (0.299*r+0.587*g+0.114*b)/255 > 0.55 ? '#1a1a1a' : '#ffffff';
}

async function loadTierlistTags() {
  try {
    const tags = await req('GET', '/api/tierlist/tags');
    allTierlistTags = tags;
    renderTierlistTagPanel();
  } catch(e) {
    document.getElementById('tl-tag-list').innerHTML = '<span class="tl-tags-loading">Теги недоступны</span>';
  }
}

function renderTierlistTagPanel() {
  const list = document.getElementById('tl-tag-list');
  const clearBtn = document.getElementById('tl-filter-clear');
  const hint = document.getElementById('tl-filter-hint');

  const query = filteredTagSearch.toLowerCase();
  const visible = allTierlistTags.filter(t =>
    !query || t.name.toLowerCase().includes(query)
  );

  if (!allTierlistTags.length) {
    list.innerHTML = '<span class="tl-tags-loading">Нет тегов</span>';
    clearBtn.style.display = 'none';
    hint.textContent = '';
    return;
  }

  const hasFilter = activeTierTagIds.size || excludedTierTagIds.size;
  clearBtn.style.display = hasFilter ? '' : 'none';

  const parts = [];
  if (activeTierTagIds.size) parts.push(`✅ включено: ${activeTierTagIds.size}`);
  if (excludedTierTagIds.size) parts.push(`🚫 исключено: ${excludedTierTagIds.size}`);
  hint.textContent = parts.length ? parts.join(' · ') : 'Клик — включить, ещё раз — исключить';

  list.innerHTML = visible.map(t => {
    const included = activeTierTagIds.has(t.id);
    const excluded = excludedTierTagIds.has(t.id);
    const cls = included ? 'active' : excluded ? 'excluded' : '';
    const prefix = included ? '✅ ' : excluded ? '🚫 ' : '';
    return `<button class="tl-tag-chip ${cls}"
      onclick="toggleTierTag(${t.id})" title="${excluded ? 'Исключить' : included ? 'Снять' : 'Включить'}">${prefix}${t.name}</button>`;
  }).join('') || '<span class="tl-tags-loading">Ничего не найдено</span>';
}

function filterTagSearch(val) {
  filteredTagSearch = val;
  renderTierlistTagPanel();
}

function toggleTierTag(id) {
  if (activeTierTagIds.has(id)) {
    // включён → исключить
    activeTierTagIds.delete(id);
    excludedTierTagIds.add(id);
  } else if (excludedTierTagIds.has(id)) {
    // исключён → нейтральный
    excludedTierTagIds.delete(id);
  } else {
    // нейтральный → включить
    activeTierTagIds.add(id);
  }
  renderTierlistTagPanel();
  loadTierlist();
}

function clearTagFilter() {
  activeTierTagIds.clear();
  excludedTierTagIds.clear();
  renderTierlistTagPanel();
  loadTierlist();
}

async function loadTierlist() {
  try {
    const tagParam = [...activeTierTagIds].join(',');
    const excludeParam = [...excludedTierTagIds].join(',');
    let url = '/api/tierlist';
    const params = [];
    if (tagParam) params.push(`tag_ids=${tagParam}`);
    if (excludeParam) params.push(`exclude_tag_ids=${excludeParam}`);
    if (params.length) url += '?' + params.join('&');
    const data = await req('GET', url);
    tierlistData = data;
    const tierMap = data.tier_map || {};
    const container = document.getElementById('tierlist-container');
    let hasAny = false;
    container.innerHTML = data.tier_order.map(tid => {
      const photos = data.tiers[tid] || [];
      const info = tierMap[tid] || {label: tid, color: '#888'};
      if (photos.length) hasAny = true;
      const textColor = textColorFor(info.color);
      return `<div class="tier-row" data-tier="${tid}">
        <div class="tier-label" style="background:${info.color};color:${textColor}">
          <span class="tier-name-full">${info.label}</span>
        </div>
        <div class="tier-photos">
          ${photos.length
            ? photos.map(p=>`<div class="tier-photo" onclick="openPhotoModal(${p.id},'${p.filename}')" title="Нажмите для просмотра">
                <img src="/photos/${p.filename}" alt="" loading="lazy">
                <div class="score-badge" style="background:${info.color};color:${textColor}">${p.vote_count}✓</div>
              </div>`).join('')
            : '<span class="tier-empty">—</span>'}
        </div>
      </div>`;
    }).join('');

    const emptyEl = document.getElementById('tierlist-empty');
    if (!hasAny) {
      emptyEl.style.display = '';
      emptyEl.innerHTML = (activeTierTagIds.size || excludedTierTagIds.size)
        ? `<div style="font-size:3rem">🔍</div><p>Нет фото с выбранными тегами.</p>`
        : `<div style="font-size:3rem">⏳</div><p>Пока нет оценённых фотографий.</p>`;
    } else {
      emptyEl.style.display = 'none';
    }
  } catch(e) { toast(e.message,'err'); }
}

function showTierlist() {
  clearPolling();
  showScreen('tierlist');
  activeTierTagIds.clear();
  excludedTierTagIds.clear();
  filteredTagSearch = '';
  const searchInput = document.getElementById('tl-filter-search');
  if (searchInput) searchInput.value = '';
  loadTierlistTags();
  loadTierlist();
}

// ── EXPORT ────────────────────────────────────────────────────────────────────

function getExportFilename(ext) {
  const hasFilt = activeTierTagIds.size || excludedTierTagIds.size;
  if (!hasFilt) return `tierlist.${ext}`;
  const inclNames = [...activeTierTagIds]
    .map(id => allTierlistTags.find(t => t.id === id)?.name || id).join('_');
  const exclNames = [...excludedTierTagIds]
    .map(id => allTierlistTags.find(t => t.id === id)?.name || id).map(n => 'no-'+n).join('_');
  const combined = [inclNames, exclNames].filter(Boolean).join('_')
    .replace(/[^a-zа-яёА-ЯЁA-Z0-9_-]/gi, '').slice(0, 40);
  return `tierlist_${combined}.${ext}`;
}

async function exportCSV() {
  if (!tierlistData) { toast('Сначала загрузите тир-лист','err'); return; }
  const tierMap = tierlistData.tier_map || {};
  const inclNote = activeTierTagIds.size
    ? 'Включены: ' + [...activeTierTagIds].map(id => allTierlistTags.find(t=>t.id===id)?.name || id).join(', ')
    : '';
  const exclNote = excludedTierTagIds.size
    ? 'Исключены: ' + [...excludedTierTagIds].map(id => allTierlistTags.find(t=>t.id===id)?.name || id).join(', ')
    : '';
  const tagNote = [inclNote, exclNote].filter(Boolean).join(' | ');
  let csv = tagNote ? `# Фильтр: ${tagNote}\n` : '';
  csv += 'Тир,Название файла,Голосов\n';
  tierlistData.tier_order.forEach(tid => {
    const label = tierMap[tid]?.label || tid;
    (tierlistData.tiers[tid] || []).forEach(p => {
      csv += `"${label}","${(p.original_name||p.filename).replace(/"/g,'""')}","${p.vote_count}"\n`;
    });
  });
  const blob = new Blob(['\uFEFF' + csv], {type:'text/csv;charset=utf-8;'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = getExportFilename('csv');
  a.click();
  toast('CSV сохранён!', 'ok');
}

async function exportPNG() {
  if (!tierlistData) { toast('Сначала загрузите тир-лист','err'); return; }
  toast('Генерирую PNG...', '');

  const tierMap = tierlistData.tier_map || {};
  const THUMB = 80, PAD = 8, LABEL_W = 100, GAP = 4;

  const rows = tierlistData.tier_order.map(tid => ({
    tid, info: tierMap[tid]||{label:tid,color:'#888'},
    photos: tierlistData.tiers[tid]||[]
  })).filter(r => r.photos.length > 0);

  if (!rows.length) { toast('Нет данных для экспорта','err'); return; }

  const loadImg = src => new Promise(res => {
    const img = new Image(); img.crossOrigin='anonymous';
    img.onload = () => res(img);
    img.onerror = () => res(null);
    img.src = src;
  });

  const allImgs = {};
  await Promise.all(rows.flatMap(r => r.photos.map(async p => {
    allImgs[p.filename] = await loadImg(`/photos/${p.filename}`);
  })));

  const inclLabel = activeTierTagIds.size
    ? 'Включены: ' + [...activeTierTagIds].map(id => allTierlistTags.find(t=>t.id===id)?.name || '').join(', ')
    : '';
  const exclLabel = excludedTierTagIds.size
    ? 'Исключены: ' + [...excludedTierTagIds].map(id => allTierlistTags.find(t=>t.id===id)?.name || '').join(', ')
    : '';
  const tagLabel = [inclLabel, exclLabel].filter(Boolean).join('  |  ');

  const maxPhotosPerRow = Math.max(...rows.map(r=>r.photos.length));
  const rowH = THUMB + PAD*2;
  const canvasW = LABEL_W + Math.min(maxPhotosPerRow, 12) * (THUMB+GAP) + PAD*2 + 40;
  const titleLines = tagLabel ? 2 : 1;
  const canvasH = rows.length * (rowH + GAP) + 52 + titleLines * 24;

  const canvas = document.createElement('canvas');
  canvas.width = canvasW; canvas.height = canvasH;
  const ctx = canvas.getContext('2d');

  ctx.fillStyle = '#0a0a0b';
  ctx.fillRect(0, 0, canvasW, canvasH);

  ctx.fillStyle = '#f0f0f0';
  ctx.font = 'bold 20px sans-serif';
  ctx.fillText('Тир-лист результатов', PAD, 30);
  if (tagLabel) {
    ctx.fillStyle = '#e8c84a';
    ctx.font = '13px sans-serif';
    ctx.fillText(tagLabel, PAD, 50);
  }

  let y = 52 + (titleLines - 1) * 22;
  for (const row of rows) {
    const tc = textColorFor(row.info.color);
    ctx.fillStyle = row.info.color;
    ctx.beginPath();
    ctx.roundRect(PAD, y, LABEL_W - PAD, rowH, 8);
    ctx.fill();
    ctx.fillStyle = tc;
    ctx.font = 'bold 14px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(row.info.label, PAD + (LABEL_W-PAD)/2, y + rowH/2 + 5);
    ctx.textAlign = 'left';

    let x = LABEL_W + PAD;
    for (const p of row.photos.slice(0, 12)) {
      const img = allImgs[p.filename];
      if (img) {
        ctx.save();
        ctx.beginPath();
        ctx.roundRect(x, y+PAD, THUMB, THUMB, 6);
        ctx.clip();
        const scale = Math.max(THUMB/img.width, THUMB/img.height);
        const sw = img.width*scale, sh = img.height*scale;
        ctx.drawImage(img, x+(THUMB-sw)/2, y+PAD+(THUMB-sh)/2, sw, sh);
        ctx.restore();
      }
      x += THUMB + GAP;
    }
    y += rowH + GAP;
  }

  canvas.toBlob(blob => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = getExportFilename('png');
    a.click();
    toast('PNG сохранён!', 'ok');
  }, 'image/png');
}


// ── VOTES LIST ────────────────────────────────────────────────────────────────
let votesListOpen = false;

function toggleVotesList() {
  votesListOpen = !votesListOpen;
  const list = document.getElementById('votes-list');
  const label = document.getElementById('votes-toggle-label');
  list.style.display = votesListOpen ? '' : 'none';
  if (votesListOpen) {
    label.textContent = 'Скрыть голоса ▴';
    loadVotesList();
  } else {
    label.textContent = 'Показать голоса ▾';
  }
}

async function loadVotesList() {
  if (!currentPhotoId) return;
  const list = document.getElementById('votes-list');
  list.innerHTML = '<div class="votes-loading">Загрузка...</div>';
  try {
    const votes = await req('GET', `/api/photo-votes/${currentPhotoId}`);
    if (!votes.length) {
      list.innerHTML = '<div class="votes-empty">Никто ещё не проголосовал</div>';
      return;
    }
    list.innerHTML = votes.map(v => `
      <div class="vote-item">
        <span class="vote-username">${v.username}</span>
        <span class="vote-tier-badge" style="background:${v.tier_color};color:${tierTextColor(v.tier_color)}">${v.tier_label}</span>
      </div>
    `).join('');
  } catch(e) {
    list.innerHTML = '<div class="votes-empty">Ошибка загрузки</div>';
  }
}

function tierTextColor(hex) {
  const r=parseInt(hex.slice(1,3),16), g=parseInt(hex.slice(3,5),16), b=parseInt(hex.slice(5,7),16);
  return (0.299*r+0.587*g+0.114*b)/255 > 0.55 ? '#1a1a1a' : '#ffffff';
}




// ── STATS SCREEN ─────────────────────────────────────────────────────────────

let selectedUserId = null;
let compareUserId = null;
let usersList = [];

function showStats() {
  clearPolling();
  showScreen('stats');
  loadUsersList();
}

async function loadUsersList() {
  try {
    usersList = await req('GET', '/api/users-list');
    const el = document.getElementById('users-list');
    if (!usersList.length) {
      el.innerHTML = '<div class="stats-empty">Нет участников</div>';
      return;
    }
    el.innerHTML = usersList.map(u => `
      <div class="user-item ${u.id === selectedUserId ? 'active' : ''}"
           onclick="selectUser(${u.id})">
        <span class="user-item-name">${u.username}${u.is_admin ? ' <span class="user-admin-badge">admin</span>' : ''}</span>
        <span class="user-item-votes">${u.vote_count} гол.</span>
      </div>`).join('');
  } catch(e) { toast(e.message, 'err'); }
}

async function selectUser(uid) {
  selectedUserId = uid;
  compareUserId = null;
  // highlight
  document.querySelectorAll('.user-item').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset?.id || el.onclick?.toString().match(/\d+/)?.[0]) === uid);
  });
  await loadUsersList(); // re-render to update active state
  await loadUserProfile(uid);
}

async function loadUserProfile(uid) {
  const el = document.getElementById('stats-content');
  el.innerHTML = '<div class="stats-loading">Загрузка...</div>';
  try {
    const d = await req('GET', `/api/user-stats/${uid}`);
    const totalVotes = d.total_votes;

    const tierBars = d.tier_counts.map(t => {
      const pct = totalVotes ? Math.round(t.count / totalVotes * 100) : 0;
      const textCol = tierTextColor(t.color.length === 7 ? t.color : '#888888');
      return `<div class="stat-tier-row">
        <span class="stat-tier-label" style="background:${t.color};color:${textCol}">${t.label}</span>
        <div class="stat-tier-bar-wrap">
          <div class="stat-tier-bar" style="width:${pct}%;background:${t.color}"></div>
        </div>
        <span class="stat-tier-count">${t.count}</span>
      </div>`;
    }).join('');

    const tagsHtml = d.fav_tags.length
      ? d.fav_tags.map(t => `<span class="stat-tag">${t.name} <span class="stat-tag-cnt">${t.cnt}</span></span>`).join('')
      : '<span class="stats-empty-sm">Нет данных</span>';

    const agreement = d.agreement_pct !== null
      ? `<div class="stat-agree">
          <div class="stat-agree-pct" style="color:${d.agreement_pct >= 60 ? 'var(--success)' : d.agreement_pct >= 40 ? 'var(--accent)' : 'var(--danger)'}">${d.agreement_pct}%</div>
          <div class="stat-agree-label">совпадение с другими<br><span style="color:var(--muted);font-size:.75rem">(по ${d.total_compared} фото с чужими оценками)</span></div>
        </div>`
      : '<span class="stats-empty-sm">Недостаточно данных</span>';

    // compare buttons — other users
    const compareList = usersList.filter(u => u.id !== uid).map(u => `
      <button class="compare-btn ${u.id === compareUserId ? 'active' : ''}"
              onclick="loadCompare(${uid}, ${u.id})">${u.username}</button>`).join('');

    el.innerHTML = `
      <div class="profile-header">
        <div class="profile-avatar">${d.user.username[0].toUpperCase()}</div>
        <div>
          <div class="profile-name">${d.user.username}</div>
          <div class="profile-sub">Оценено фото: <b>${totalVotes}</b></div>
        </div>
      </div>

      <div class="stats-grid-2">
        <div class="stat-card">
          <div class="stat-card-title">Распределение оценок</div>
          <div class="stat-tier-bars">${tierBars}</div>
        </div>
        <div class="stat-card">
          <div class="stat-card-title">Совпадение с большинством</div>
          ${agreement}
        </div>
      </div>

      <div class="stat-card" style="margin-top:12px">
        <div class="stat-card-title">Любимые теги (топ фото)</div>
        <div class="stat-tags-wrap">${tagsHtml}</div>
      </div>

      ${usersList.filter(u => u.id !== uid).length ? `
      <div class="stat-card" style="margin-top:12px">
        <div class="stat-card-title">Сравнить с пользователем</div>
        <div class="compare-btns">${compareList}</div>
        <div id="compare-result"></div>
      </div>` : ''}
    `;
  } catch(e) {
    el.innerHTML = `<div class="stats-empty">${e.message}</div>`;
  }
}

async function loadCompare(uid1, uid2) {
  compareUserId = uid2;
  // re-render compare buttons
  document.querySelectorAll('.compare-btn').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.trim() === usersList.find(u => u.id === uid2)?.username);
  });
  const el = document.getElementById('compare-result');
  if (!el) return;
  el.innerHTML = '<div class="stats-loading" style="margin-top:10px">Загрузка...</div>';
  try {
    const d = await req('GET', `/api/compare/${uid1}/${uid2}`);
    const simColor = d.similarity >= 70 ? 'var(--success)' : d.similarity >= 40 ? 'var(--accent)' : 'var(--danger)';

    const disHtml = d.disagreements.length ? d.disagreements.map(p => `
      <div class="disagree-row">
        <img src="/photos/${p.filename}" class="disagree-img" onclick="openPhotoModal(${p.photo_id},'${p.filename}')">
        <div class="disagree-tiers">
          <span class="modal-tier-badge" style="background:${p.color1};color:${tierTextColor(p.color1)}">${p.label1}</span>
          <span style="color:var(--muted);font-size:.8rem">vs</span>
          <span class="modal-tier-badge" style="background:${p.color2};color:${tierTextColor(p.color2)}">${p.label2}</span>
        </div>
      </div>`).join('')
    : '<span class="stats-empty-sm">Разногласий нет</span>';

    el.innerHTML = `
      <div class="compare-summary">
        <div class="compare-sim" style="color:${simColor}">${d.similarity}%<span>схожесть</span></div>
        <div class="compare-nums">
          <span>✅ Совпало: <b>${d.exact_match}</b></span>
          <span>〰️ Близко: <b>${d.close_match}</b></span>
          <span>❌ Расхождение: <b>${d.disagreements.length}</b></span>
          <span style="color:var(--muted)">Общих фото: ${d.common_photos}</span>
        </div>
      </div>
      ${d.disagreements.length ? `<div class="stat-card-title" style="margin:10px 0 6px">Главные расхождения</div>
      <div class="disagree-list">${disHtml}</div>` : ''}
    `;
  } catch(e) {
    el.innerHTML = `<div class="stats-empty">${e.message}</div>`;
  }
}

// ── PHOTO MODAL ───────────────────────────────────────────────────────────────

async function openPhotoModal(photoId, filename) {
  const modal = document.getElementById('photo-modal');
  const img = document.getElementById('modal-img');
  const fname = document.getElementById('modal-filename');
  const votesEl = document.getElementById('modal-votes');
  const tagsEl = document.getElementById('modal-tags');

  img.src = `/photos/${filename}`;
  fname.textContent = '';
  votesEl.innerHTML = '<div class="modal-loading">Загрузка...</div>';
  tagsEl.innerHTML = '';
  modal.style.display = 'flex';
  document.body.style.overflow = 'hidden';

  try {
    const d = await req('GET', `/api/photo-detail/${photoId}`);
    fname.textContent = d.photo.original_name || d.photo.filename;

    // votes
    if (!d.votes.length) {
      votesEl.innerHTML = '<div class="modal-empty">Нет оценок</div>';
    } else {
      votesEl.innerHTML = d.votes.map(v => {
        const color = v.tier_color || '#888';
        const textCol = tierTextColor(color.length === 7 ? color : '#888888');
        return `<div class="modal-vote-row">
          <span class="modal-tier-badge" style="background:${color};color:${textCol}">${v.tier_label || v.tier_id}</span>
          <span class="modal-username">${v.username}</span>
        </div>`;
      }).join('');
    }

    // tags
    if (!d.tags.length) {
      tagsEl.innerHTML = '<div class="modal-empty">Нет тегов</div>';
    } else {
      tagsEl.innerHTML = d.tags.map(t => `
        <span class="modal-tag" title="${t.users}">
          ${t.name}
          <span class="modal-tag-cnt">${t.count}</span>
        </span>`).join('');
    }
  } catch(e) {
    votesEl.innerHTML = '<div class="modal-empty">Ошибка загрузки</div>';
  }
}

function closePhotoModal() {
  document.getElementById('photo-modal').style.display = 'none';
  document.body.style.overflow = '';
}

function closeModal(e) {
  if (e.target === document.getElementById('photo-modal')) closePhotoModal();
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closePhotoModal(); });



// ── TAGS ──────────────────────────────────────────────────────────────────────
let myTags = [];        // tag objects {id, name} set by current user
let allPhotoTags = [];  // all users tags [{username, tag_name, tag_id}]
let dropdownVisible = false;

async function loadTags() {
  if (!currentPhotoId) return;
  try {
    const data = await req('GET', `/api/photo-tags/${currentPhotoId}`);
    allPhotoTags = data.all || [];
    myTags = data.mine || [];
    renderTags();
  } catch(e) {}
}

function renderTags() {
  const sel = document.getElementById('tags-selected');
  if (!sel) return;

  // my tags as removable chips
  const myHtml = myTags.map(t => `
    <span class="tag-chip mine" title="Нажмите чтобы удалить">
      ${t.name}
      <span class="tag-remove" onclick="removeTag(${t.id})">×</span>
    </span>`).join('');

  // other users' tags grouped
  const others = {};
  allPhotoTags.forEach(r => {
    if (!others[r.tag_name]) others[r.tag_name] = [];
    others[r.tag_name].push(r.username);
  });
  const myTagNames = new Set(myTags.map(t => t.name));
  const othersHtml = Object.entries(others)
    .filter(([name]) => !myTagNames.has(name))
    .map(([name, users]) => `
      <span class="tag-chip other" title="${users.join(', ')}: ${name}"
            onclick="addTagByName('${name.replace(/'/g,"\'")}')">
        ${name} <span class="tag-users">${users.length}</span>
      </span>`).join('');

  sel.innerHTML = myHtml + othersHtml;
}

function searchTags(q) {
  const dropdown = document.getElementById('tags-dropdown');
  if (!q.trim()) { dropdown.style.display = 'none'; return; }
  const lower = q.toLowerCase().replace(/ /g,'_');
  const results = ALL_TAGS
    .filter(t => t.toLowerCase().includes(lower))
    .slice(0, 30);
  if (!results.length) { dropdown.style.display = 'none'; return; }
  const myTagNames = new Set(myTags.map(t => t.name));
  dropdown.innerHTML = results.map(t => `
    <div class="tag-option ${myTagNames.has(t) ? 'already' : ''}"
         onclick="addTagByName('${t.replace(/'/g,"\'")}')">
      ${t}${myTagNames.has(t) ? ' ✓' : ''}
    </div>`).join('');
  dropdown.style.display = '';
}

function showDropdown() {
  const q = document.getElementById('tags-input')?.value;
  if (q) searchTags(q);
}

async function addTagByName(name) {
  if (!currentPhotoId) return;
  if (myTags.find(t => t.name === name)) return;
  try {
    await formReq(`/api/photo-tags/${currentPhotoId}/add`, {tag_name: name});
    document.getElementById('tags-input').value = '';
    document.getElementById('tags-dropdown').style.display = 'none';
    await loadTags();
  } catch(e) { toast(e.message || 'Ошибка', 'err'); }
}

async function removeTag(tagId) {
  if (!currentPhotoId) return;
  try {
    await req('DELETE', `/api/photo-tags/${currentPhotoId}/${tagId}`);
    await loadTags();
  } catch(e) { toast(e.message, 'err'); }
}

// close dropdown on outside click
document.addEventListener('click', e => {
  if (!e.target.closest('.tags-search-wrap')) {
    const d = document.getElementById('tags-dropdown');
    if (d) d.style.display = 'none';
  }
});

// ── YANDEX DISK AUTO-SYNC ─────────────────────────────────────────────────────
async function loadWatchStatus() {
  try {
    const d = await req('GET', '/api/admin/yadisk-watch');
    const status = document.getElementById('watch-status');
    const syncBtn = document.getElementById('watch-sync-now-btn');
    const delBtn = document.getElementById('watch-delete-btn');
    if (!d) {
      status.innerHTML = '<span class="watch-off">⬤ Выключено</span>';
      syncBtn.style.display = 'none';
      delBtn.style.display = 'none';
      return;
    }
    document.getElementById('watch-url').value = d.public_url || '';
    const sel = document.getElementById('watch-interval');
    [...sel.options].forEach(o => { if (parseInt(o.value) === d.interval_minutes) o.selected = true; });
    const lastSync = d.last_sync_at
      ? new Date(d.last_sync_at + 'Z').toLocaleString('ru')
      : 'ещё не было';
    const added = d.last_sync_added ?? 0;
    const errors = d.last_sync_errors ?? 0;
    const nextSync = d.last_sync_at
      ? new Date(new Date(d.last_sync_at + 'Z').getTime() + d.interval_minutes * 60000).toLocaleString('ru')
      : 'скоро';
    let syncInfo = added === -1
      ? `<span class="watch-err">ошибка последней синхронизации</span>`
      : `добавлено: <b>${added}</b>${errors ? `, ошибок: ${errors}` : ''}`;
    status.innerHTML = `<span class="watch-on">⬤ Активно</span> · проверка каждые <b>${d.interval_minutes} мин</b><br>
      <span class="watch-meta">Последняя: ${lastSync} (${syncInfo})</span><br>
      <span class="watch-meta">Следующая: ${nextSync}</span>`;
    syncBtn.style.display = '';
    delBtn.style.display = '';
  } catch(e) {
    document.getElementById('watch-status').textContent = '';
  }
}

async function saveWatchUrl() {
  const url = document.getElementById('watch-url').value.trim();
  if (!url) { toast('Введите ссылку', 'err'); return; }
  const interval = document.getElementById('watch-interval').value;
  try {
    await formReq('/api/admin/yadisk-watch', { public_url: url, interval_minutes: interval });
    toast('Авто-синхронизация включена! Первая проверка запущена.', 'ok');
    loadWatchStatus();
  } catch(e) { toast(e.message || 'Ошибка', 'err'); }
}

async function deleteWatchUrl() {
  if (!confirm('Отключить авто-синхронизацию?')) return;
  try {
    await req('DELETE', '/api/admin/yadisk-watch');
    toast('Авто-синхронизация отключена', 'ok');
    document.getElementById('watch-url').value = '';
    loadWatchStatus();
  } catch(e) { toast(e.message, 'err'); }
}

async function watchSyncNow() {
  const btn = document.getElementById('watch-sync-now-btn');
  btn.disabled = true; btn.textContent = '↻ Проверяю...';
  try {
    const d = await req('POST', '/api/admin/yadisk-watch/sync-now');
    toast(`Готово: добавлено ${d.added}, пропущено ${d.skipped}` + (d.errors ? `, ошибок ${d.errors}` : ''), 'ok');
    loadWatchStatus();
    if (d.added > 0) loadPhotos();
  } catch(e) { toast(e.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '↻ Проверить сейчас'; }
}

// ── YANDEX DISK IMPORT ────────────────────────────────────────────────────────
async function previewYadisk() {
  const url = document.getElementById('yadisk-url').value.trim();
  if (!url) { toast('Введите ссылку на папку Яндекс.Диска', 'err'); return; }
  const preview = document.getElementById('yadisk-preview');
  const importBtn = document.getElementById('yadisk-import-btn');
  preview.style.display = '';
  preview.textContent = 'Загрузка списка файлов...';
  importBtn.style.display = 'none';
  try {
    const data = await req('GET', '/api/admin/preview-yadisk?public_url=' + encodeURIComponent(url));
    if (data.total === 0) {
      preview.textContent = 'Файлов не найдено. Проверьте ссылку и доступ к папке.';
      return;
    }
    let html = `<div class="yadisk-summary">Найдено: ${data.total} фото, новых: <b>${data.new}</b></div>`;
    html += '<div class="yadisk-file-list">';
    data.files.forEach(f => {
      const kb = Math.round(f.size / 1024);
      const rowClass = f.exists ? 'exists' : 'new';
      const status = f.exists ? 'уже есть' : 'новое';
      html += `<div class="yadisk-file-row ${rowClass}">
        <span class="yadisk-file-name">${f.name}</span>
        <span class="yadisk-file-size">${kb} KB</span>
        <span class="yadisk-file-status">${status}</span>
      </div>`;
    });
    if (data.total > data.files.length) html += `<div class="yadisk-more">… и ещё ${data.total - data.files.length} файлов</div>`;
    html += '</div>';
    preview.innerHTML = html;
    if (data.new > 0) importBtn.style.display = '';
  } catch(e) {
    preview.innerHTML = `<div class="yadisk-error">Ошибка: ${e.message || e}</div>`;
    importBtn.style.display = 'none';
  }
}

async function importYadisk() {
  const url = document.getElementById('yadisk-url').value.trim();
  if (!url) { toast('Введите ссылку на папку Яндекс.Диска', 'err'); return; }
  const progress = document.getElementById('yadisk-progress');
  const label = document.getElementById('yadisk-label');
  const fill = document.getElementById('yadisk-fill');
  const importBtn = document.getElementById('yadisk-import-btn');
  importBtn.style.display = 'none';
  progress.style.display = '';
  label.textContent = 'Идёт импорт, подождите...';
  fill.style.width = '100%';
  try {
    const fd = new FormData();
    fd.append('public_url', url);
    const resp = await fetch(API + '/api/admin/import-yadisk', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token },
      body: fd
    });
    const text = await resp.text();
    let json; try { json = JSON.parse(text); } catch { json = text; }
    if (!resp.ok) throw new Error(json?.detail || json || resp.statusText);
    label.textContent = `Готово! Добавлено: ${json.added}, пропущено: ${json.skipped}, ошибок: ${json.errors}`;
    fill.style.animation = 'none';
    fill.style.width = '100%';
    setTimeout(() => { progress.style.display = 'none'; }, 4000);
    loadPhotos();
  } catch(e) {
    label.textContent = 'Ошибка: ' + (e.message || e);
    fill.style.width = '0%';
    setTimeout(() => { progress.style.display = 'none'; importBtn.style.display = ''; }, 4000);
  }
}

// ── HTTP ──────────────────────────────────────────────────────────────────────
async function req(method, url, body) {
  const opts={method,headers:{'Authorization':'Bearer '+token}};
  if(body){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}
  const resp=await fetch(API+url,opts);
  const text=await resp.text(); let json; try{json=JSON.parse(text);}catch{json=text;}
  if(!resp.ok) throw new Error(json?.detail||json||resp.statusText);
  return json;
}
async function formReq(url,fields) {
  const fd=new FormData(); Object.entries(fields).forEach(([k,v])=>fd.append(k,v));
  const resp=await fetch(API+url,{method:'POST',headers:token?{'Authorization':'Bearer '+token}:{},body:fd});
  const text=await resp.text(); let json; try{json=JSON.parse(text);}catch{json=text;}
  if(!resp.ok) throw new Error(json?.detail||json||resp.statusText);
  return json;
}
let toastTimer;
function toast(msg,type=''){
  const el=document.getElementById('toast');
  el.textContent=msg; el.className='toast show'+(type?' '+type:'');
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.classList.remove('show'),3500);
}
