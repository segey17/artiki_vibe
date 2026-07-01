const API = '';
let token = localStorage.getItem('token') || null;
let me = null;
let currentSessionId = null;     // id сессии, в которой сейчас находится пользователь (голосование/тир-лист/настройки)
let currentSessionInfo = null;   // последний полученный /api/sessions/{id} — для is_owner, title и т.п.
let currentPhotoId = null;
let selectedTierId = null;
let tiers = [];
let pollTimer = null;
let ws = null;
let wsReconnectTimer = null;
let autoAdvanceEnabled = false;

// ── THEME ────────────────────────────────────────────────────────────────
// Тема применяется максимально рано (см. инлайн-скрипт в <head> index.html),
// здесь только переключение по клику и синхронизация иконок кнопок.
function applyThemeIcons() {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  document.querySelectorAll('.theme-toggle-icon-target')
    .forEach(btn => { btn.textContent = isDark ? '☀️' : '🌙'; });
}
function toggleTheme() {
  const root = document.documentElement;
  const isDark = root.getAttribute('data-theme') === 'dark';
  const next = isDark ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  try { localStorage.setItem('theme', next); } catch {}
  applyThemeIcons();
}
applyThemeIcons();

// ── PHOTO CACHE / PRELOADING ───────────────────────────────────────────────
// Кэш уже загруженных изображений (filename -> Image), чтобы при перелистывании
// фото не было "ожидания" — браузер отдаёт картинку из памяти/кэша мгновенно.
const photoImgCache = new Map();
let currentPhotoFilename = null;     // имя файла текущего показанного фото
let pendingNavDirection = null;      // 'next' | 'prev' | null — куда листаем (для анимации)

function preloadPhoto(filename) {
  if (!filename || photoImgCache.has(filename)) return;
  const img = new Image();
  img.src = `/photos/${filename}`;
  photoImgCache.set(filename, img);
}

// Меняет фото в #current-photo-img с анимацией перелистывания.
// Если картинка уже в кэше (была предзагружена) — браузер берёт её
// из памяти и показывает мгновенно, без "мигания"/ожидания.
function setPhotoImage(filename) {
  const frame = document.getElementById('photo-frame') || document.querySelector('.photo-frame');
  const imgEl = document.getElementById('current-photo-img');
  if (!imgEl) return;

  const direction = pendingNavDirection; // 'next' | 'prev' | null
  pendingNavDirection = null;

  const url = `/photos/${filename}`;
  currentPhotoFilename = filename;

  // сбрасываем индикатор ошибки от предыдущего фото
  imgEl.classList.remove('img-error');
  const errHint = document.getElementById('photo-error-hint');
  if (errHint) errHint.style.display = 'none';
  imgEl.onerror = () => {
    imgEl.classList.add('img-error');
    if (errHint) errHint.style.display = '';
  };

  // гарантируем, что картинка есть в кэше браузера (создаёт Image, если не было)
  preloadPhoto(filename);

  const outClass = direction === 'prev' ? 'photo-anim-out-right' : 'photo-anim-out-left';
  const inClass  = direction === 'prev' ? 'photo-anim-in-left'  : 'photo-anim-in-right';

  if (!direction || !frame) {
    // первая загрузка / неизвестное направление — без анимации выезда
    imgEl.src = url;
    return;
  }

  frame.classList.remove('photo-anim-out-left', 'photo-anim-out-right', 'photo-anim-in-left', 'photo-anim-in-right');
  frame.classList.add(outClass);

  const swap = () => {
    imgEl.src = url;
    frame.classList.remove(outClass);
    frame.classList.add(inClass);
    setTimeout(() => frame.classList.remove(inClass), 260);
  };

  // ждём конца анимации "выезда", затем подставляем новую картинку и "въезжаем"
  setTimeout(swap, 180);
}

document.addEventListener('DOMContentLoaded', async () => {
  if (token) {
    try {
      me = await req('GET', '/api/me');
      afterAuthSuccess();
    } catch { token = null; localStorage.removeItem('token'); showScreen('auth'); }
  } else { showScreen('auth'); }
});

// ── SCREENS ──────────────────────────────────────────────────────────────────
function showScreen(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.getElementById('screen-' + name)?.classList.add('active');
}

function showSessionsList() {
  clearPolling();
  currentSessionId = null;
  showScreen('sessions');
  document.getElementById('sessions-nav-username').textContent = me?.username || '';
  loadSessionsList();
  try { history.replaceState(null, '', location.pathname); } catch {}
}

async function showVote(sessionId) {
  if (sessionId) currentSessionId = sessionId;
  if (!currentSessionId) { showSessionsList(); return; }
  showScreen('vote');
  try {
    currentSessionInfo = await req('GET', `/api/sessions/${currentSessionId}`);
    document.getElementById('nav-session-title').textContent = currentSessionInfo.title || '';
    document.getElementById('btn-session-admin').style.display = currentSessionInfo.is_owner ? '' : 'none';
    document.getElementById('admin-controls').style.display = currentSessionInfo.is_owner ? '' : 'none';
    try { history.replaceState(null, '', '#' + currentSessionInfo.code); } catch {}
  } catch (e) {
    toast(e.message || 'Сессия недоступна', 'err');
    showSessionsList();
    return;
  }
  loadCurrentPhoto(); startPolling();
  connectWS();
  if (currentSessionInfo.is_owner) loadAutoAdvanceState();
}

function showSessionAdmin() {
  if (!currentSessionId) return;
  clearPolling();
  showScreen('session-admin');
  renderSessionTierEditor();
}

function showPhotoManager() {
  clearPolling();
  showScreen('photo-manager');
  document.getElementById('reset-db-card').style.display = me?.is_admin ? '' : 'none';
  loadPhotos(); loadAdminStats(); loadWatchStatus(); loadGdriveWatchStatus(); loadWd14Status(); checkDuplicatePhotos();
}

// ── AUTH ─────────────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach((b,i) => b.classList.toggle('active',(i===0)===(tab==='login')));
  document.getElementById('form-login').classList.toggle('hidden', tab!=='login');
  document.getElementById('form-register').classList.toggle('hidden', tab!=='register');
}
async function afterAuthSuccess() {
  // После входа: если в ссылке есть код сессии (#abc123) — сразу заходим в неё,
  // иначе показываем галерею — она теперь стартовый экран сайта.
  const codeFromHash = location.hash?.replace('#', '').trim();
  if (codeFromHash) {
    try {
      const r = await req('GET', `/api/sessions/by-code/${encodeURIComponent(codeFromHash)}`);
      showVote(r.session_id);
      return;
    } catch (e) {
      toast('Сессия по ссылке не найдена', 'err');
    }
  }
  showGallery();
}
async function doLogin(e) {
  e.preventDefault();
  try {
    const d = await formReq('/api/login', {
      username: document.getElementById('login-username').value,
      password: document.getElementById('login-password').value });
    token = d.token; localStorage.setItem('token', token);
    me = {username: d.username, is_admin: d.is_admin};
    afterAuthSuccess();
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
    afterAuthSuccess();
  } catch(e) { document.getElementById('reg-error').textContent = e.message; }
}
function logout() {
  clearPolling(); token = null; me = null; currentSessionId = null;
  localStorage.removeItem('token'); showScreen('auth');
}

// ── SESSIONS LIST & CREATE ───────────────────────────────────────────────────
async function loadSessionsList() {
  const el = document.getElementById('sessions-list');
  el.innerHTML = '<div class="sessions-empty">Загрузка...</div>';
  try {
    const list = await req('GET', '/api/sessions');
    if (!list.length) {
      el.innerHTML = '<div class="sessions-empty">Активных сессий пока нет — создайте первую.</div>';
      return;
    }
    el.innerHTML = list.map(s => `
      <div class="session-card" onclick="showVote(${s.id})">
        <div class="session-card-main">
          <div class="session-card-title">${escapeHtml(s.title)}</div>
          <div class="session-card-meta">от ${escapeHtml(s.creator_username)} · ${s.photo_count} фото · ${s.participant_count} участников</div>
        </div>
        <span class="session-card-badge ${s.voting_open ? 'open' : 'closed'}">${s.voting_open ? 'идёт' : 'закрыта'}</span>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = `<div class="sessions-empty">Ошибка загрузки: ${e.message}</div>`;
  }
}

async function joinByCode() {
  const code = document.getElementById('join-code-input').value.trim();
  if (!code) { toast('Введите код сессии', 'err'); return; }
  try {
    const r = await req('GET', `/api/sessions/by-code/${encodeURIComponent(code)}`);
    showVote(r.session_id);
  } catch (e) { toast(e.message || 'Сессия не найдена', 'err'); }
}

let newSessionTiers = [];

function openCreateSessionForm() {
  newSessionTiers = JSON.parse(JSON.stringify(DEFAULT_TIERS_TEMPLATE));
  document.getElementById('new-session-title').value = '';
  document.getElementById('shuffle-check').checked = true;
  document.getElementById('tag-card-search').value = '';
  document.getElementById('include-suggestions-check').checked = false;
  renderNewSessionTierEditor();
  loadTagOptionsForSessionCreate();
  showScreen('create-session');
}

let allTagsForSessionCreate = [];      // топ-10 тегов от /api/tierlist/tags (с превью, is_favorite, has_confirmed)
let allPhotosPreview = null;           // {filename} — случайное превью для плитки "Все фото"
let selectedAlbumId = 'all';           // 'all' | tagId — что выбрано в сетке альбомов сейчас

async function loadTagOptionsForSessionCreate() {
  const grid = document.getElementById('tag-card-grid');
  grid.innerHTML = '<div class="tag-card-empty">Загрузка альбомов...</div>';
  selectedAlbumId = 'all';
  document.getElementById('tag-card-search').value = '';
  try {
    const [tagsList, allPreview] = await Promise.all([
      req('GET', `/api/tierlist/tags?include_suggestions=${includeSuggestionsFlag()}`),
      allPhotosPreview ? Promise.resolve(allPhotosPreview) : req('GET', '/api/photos/random-preview')
    ]);
    allTagsForSessionCreate = tagsList;
    allPhotosPreview = allPreview;
    renderTagCardGrid('');
  } catch {
    grid.innerHTML = '<div class="tag-card-empty">Ошибка загрузки альбомов</div>';
  }
}

function includeSuggestionsFlag() {
  return document.getElementById('include-suggestions-check').checked;
}

let tagCardSearchDebounce = null;

function onTagCardSearchInput(value) {
  // Без запроса — мгновенно показываем кэш (топ-10, уже на руках).
  // С запросом — идём на backend искать по ВСЕМ тегам сайта (не только
  // витрине из топ-10), с небольшой задержкой, чтобы не слать запрос на
  // каждое нажатие клавиши.
  clearTimeout(tagCardSearchDebounce);
  const q = (value || '').trim();
  if (!q) { renderTagCardGrid(''); return; }
  tagCardSearchDebounce = setTimeout(() => searchTagCardsRemote(q), 250);
}

async function searchTagCardsRemote(q) {
  const grid = document.getElementById('tag-card-grid');
  try {
    const results = await req('GET',
      `/api/tierlist/tags?include_suggestions=${includeSuggestionsFlag()}&q=${encodeURIComponent(q)}`);
    renderTagCardGrid(q, results);
  } catch {
    grid.innerHTML = '<div class="tag-card-empty">Ошибка поиска</div>';
  }
}

function renderTagCardGrid(query, searchResults) {
  const grid = document.getElementById('tag-card-grid');
  const q = (query || '').trim().toLowerCase();

  // Плитка "Все фото" — всегда первая, видна и при поиске (если запрос не задан
  // или совпадает с её названием), как обычный альбом в галерее.
  const allCardMatches = !q || 'все фото'.includes(q);

  let tagCards;
  if (!q) {
    // без поиска — витрина топ-10, уже на руках, без лимита (backend уже
    // прислал ровно столько, сколько нужно показать); урезаем до 9, чтобы
    // вместе с плиткой "Все фото" было ровно 10.
    tagCards = allTagsForSessionCreate.slice(0, 9);
  } else {
    // с поиском — список уже пришёл с backend по полному набору тегов сайта,
    // никакого дополнительного урезания не делаем.
    tagCards = searchResults || [];
  }

  if (!allCardMatches && !tagCards.length) {
    grid.innerHTML = '<div class="tag-card-empty">Ничего не найдено</div>';
    return;
  }

  let html = '';
  if (allCardMatches) {
    html += `
      <div class="tag-card ${selectedAlbumId === 'all' ? 'selected' : ''}" data-album-id="all" onclick="selectAlbumCard('all')" title="Все фото">
        ${allPhotosPreview?.filename
          ? `<img src="/photos/${allPhotosPreview.filename}" alt="" loading="lazy">`
          : `<div class="tag-card-noimg">🖼️</div>`}
        <div class="tag-card-overlay">Все фото</div>
      </div>`;
  }
  html += tagCards.map(t => `
    <div class="tag-card ${t.id === selectedAlbumId ? 'selected' : ''}" data-album-id="${t.id}" onclick="selectAlbumCard(${t.id})" title="${escapeHtml(t.name)}">
      ${t.preview_filename
        ? `<img src="/photos/${t.preview_filename}" alt="" loading="lazy">`
        : `<div class="tag-card-noimg">🏷️</div>`}
      ${t.is_favorite ? '<span class="tag-card-fav-badge" title="Один из ваших любимых тегов">⭐</span>' : ''}
      ${!t.has_confirmed ? '<span class="tag-card-ai-badge">только AI</span>' : ''}
      <div class="tag-card-overlay">${escapeHtml(t.name)} <span class="tag-card-count">${t.photo_count}</span></div>
    </div>
  `).join('');

  grid.innerHTML = html;
}

function selectAlbumCard(albumId) {
  selectedAlbumId = albumId;
  document.querySelectorAll('.tag-card').forEach(el => {
    el.classList.toggle('selected', String(el.dataset.albumId) === String(albumId));
  });
}

async function submitCreateSession() {
  const title = document.getElementById('new-session-title').value.trim();
  const isAllPhotos = selectedAlbumId === 'all';
  const includeSuggestions = document.getElementById('include-suggestions-check').checked;
  const shuffle = document.getElementById('shuffle-check').checked;

  if (newSessionTiers.length < 2) { toast('Нужно минимум 2 тира', 'err'); return; }
  if (!isAllPhotos && !selectedAlbumId) { toast('Выберите альбом', 'err'); return; }

  try {
    const body = {
      title, tiers: newSessionTiers,
      photo_filter: isAllPhotos ? 'all' : 'tag',
      tag_id: isAllPhotos ? null : selectedAlbumId,
      include_suggestions: includeSuggestions, shuffle
    };
    const resp = await fetch(API + '/api/sessions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
      body: JSON.stringify(body)
    });
    const json = await resp.json();
    if (!resp.ok) throw new Error(json.detail || 'Ошибка создания сессии');
    toast(`Сессия создана: ${json.photo_count} фото`, 'ok');
    showVote(json.id);
  } catch (e) {
    toast(e.message || 'Ошибка', 'err');
  }
}

async function endCurrentSession() {
  if (!currentSessionId) return;
  if (!confirm('Завершить сессию? Она исчезнет из списка активных, но данные сохранятся.')) return;
  try {
    await req('POST', `/api/sessions/${currentSessionId}/end`);
    toast('Сессия завершена', 'ok');
    showSessionsList();
  } catch (e) { toast(e.message, 'err'); }
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s ?? '';
  return d.innerHTML;
}

// ── VOTING ────────────────────────────────────────────────────────────────────
async function loadCurrentPhoto() {
  if (!currentSessionId) return;
  try {
    const data = await req('GET', `/api/sessions/${currentSessionId}/current-photo`);
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
  const photoChanged = currentPhotoId !== photo.id;
  if (photoChanged) {
    votesListOpen = false;
    const list = document.getElementById('votes-list');
    const label = document.getElementById('votes-toggle-label');
    if (list) list.style.display = 'none';
    if (label) label.textContent = 'Показать голоса ▾';
  }
  currentPhotoId = photo.id;

  if (photoChanged) {
    setPhotoImage(photo.filename);
  }

  // предзагружаем соседние фото в кэш, чтобы следующее перелистывание было мгновенным
  preloadPhoto(data.next_filename);
  preloadPhoto(data.prev_filename);

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
  if (currentSessionInfo?.is_owner)
    document.getElementById('admin-stats').textContent = `Голосов: ${voteCount}`;
}

function renderVoteBtns(activeId, locked, counts = {}) {
  document.getElementById('tier-vote-btns').innerHTML = tiers.map(t => {
    const cnt = counts[t.id] || 0;
    const isActive = t.id === activeId;
    return `<button class="tier-vote-btn ${isActive?'active':''}"
      data-id="${t.id}"
      style="--tc:${t.color}"
      onclick="${locked ? '' : `selectTier('${t.id}', event)`}"
      ${locked ? 'disabled' : ''}>
      <span class="tvb-swatch"></span>
      <span class="tvb-label">${t.label}</span>
      ${cnt ? `<span class="tvb-count">${cnt}</span>` : ''}
    </button>`;
  }).join('');
}

function selectTier(id, evt) {
  selectedTierId = id;
  renderVoteBtns(id, false);
  const t = tiers.find(t => t.id === id);
  const btn = document.getElementById('btn-vote');
  btn.disabled = false;
  btn.textContent = `Поставить: ${t?.label || id}`;

  // ripple-вспышка от точки клика
  const target = evt?.currentTarget || document.querySelector(`.tier-vote-btn[data-id="${id}"]`);
  if (target) {
    const rect = target.getBoundingClientRect();
    const x = (evt?.clientX ?? rect.left + rect.width/2) - rect.left;
    const y = (evt?.clientY ?? rect.top + rect.height/2) - rect.top;
    const ripple = document.createElement('span');
    ripple.className = 'tvb-ripple';
    ripple.style.left = x + 'px';
    ripple.style.top = y + 'px';
    ripple.style.width = ripple.style.height = '14px';
    ripple.style.marginLeft = ripple.style.marginTop = '-7px';
    target.appendChild(ripple);
    setTimeout(() => ripple.remove(), 600);
  }
}

async function submitVote() {
  if (!selectedTierId || !currentPhotoId || !currentSessionId) return;
  try {
    await formReq(`/api/sessions/${currentSessionId}/rate`, {photo_id: currentPhotoId, tier_id: selectedTierId});
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
  if (!token || !currentSessionId) return;
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}&session_id=${currentSessionId}`);

  ws.onopen = () => {
    console.log('WS connected');
  };

  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'photo_change' || msg.type === 'voting_closed' || msg.type === 'vote_update') {
        selectedTierId = null;
        if (msg.type === 'photo_change') pendingNavDirection = msg.direction || 'next';
        loadCurrentPhoto();
      }
      if (msg.type === 'vote_update' || msg.type === 'photo_change') {
        if (currentSessionInfo?.is_owner) loadOnlineUsers();
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
        if (currentSessionInfo?.is_owner) toast('Все фото просмотрены!', 'ok');
      }
    } catch {}
  };

  ws.onclose = () => {
    console.log('WS disconnected, reconnecting in 3s...');
    wsReconnectTimer = setTimeout(connectWS, 3000);
  };

  ws.onerror = () => { ws.close(); };
}

// ── SESSION ADMIN NAV (управление текущей сессией — доступно только владельцу) ──
async function nextPhoto() {
  if (!currentSessionId) return;
  try {
    const d = await req('POST', `/api/sessions/${currentSessionId}/next-photo`);
    if (d.done) toast('Все фотографии просмотрены!', 'ok');
    else { selectedTierId = null; pendingNavDirection = 'next'; loadCurrentPhoto(); }
  } catch(e) { toast(e.message,'err'); }
}
async function prevPhoto() {
  if (!currentSessionId) return;
  try { await req('POST', `/api/sessions/${currentSessionId}/prev-photo`); selectedTierId=null; pendingNavDirection = 'prev'; loadCurrentPhoto(); }
  catch(e) { toast(e.message||'Уже первая','err'); }
}
async function closeVoting() {
  if (!currentSessionId) return;
  try { await req('POST', `/api/sessions/${currentSessionId}/close-voting`); toast('Закрыто','ok'); loadCurrentPhoto(); }
  catch(e) { toast(e.message,'err'); }
}

async function shufflePhotos() {
  if (!currentSessionId) return;
  if (!confirm('Перемешать все фотографии в случайном порядке и начать с первой?')) return;
  try {
    const d = await req('POST', `/api/sessions/${currentSessionId}/shuffle`);
    toast(`Перемешано ${d.count} фото 🔀`, 'ok');
    selectedTierId = null;
    loadCurrentPhoto();
  } catch(e) { toast(e.message, 'err'); }
}

async function toggleAutoAdvance() {
  if (!currentSessionId) return;
  try {
    const newState = !autoAdvanceEnabled;
    await formReq(`/api/sessions/${currentSessionId}/auto-advance`, { enabled: newState });
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
  if (!currentSessionInfo?.is_owner || !currentSessionId) return;
  try {
    const d = await req('GET', `/api/sessions/${currentSessionId}/auto-advance`);
    autoAdvanceEnabled = d.enabled;
    updateAutoAdvanceBtn();
    if (d.enabled) loadOnlineUsers();
  } catch {}
}

async function loadOnlineUsers() {
  if (!currentSessionInfo?.is_owner || !currentSessionId) return;
  const bar = document.getElementById('online-users-bar');
  if (!autoAdvanceEnabled) { bar.style.display = 'none'; return; }
  try {
    const d = await req('GET', `/api/sessions/${currentSessionId}/online-users`);
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
    if (currentSessionInfo?.is_owner) { e.preventDefault(); nextPhoto(); }
  } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
    if (currentSessionInfo?.is_owner) { e.preventDefault(); prevPhoto(); }
  } else if (e.key === ' ' || e.key === 'Enter') {
    // пробел/Enter — подтвердить оценку
    const btn = document.getElementById('btn-vote');
    if (btn && !btn.disabled) { e.preventDefault(); submitVote(); }
  }
});

// ── TIER EDITOR ───────────────────────────────────────────────────────────────
const PALETTE = ['#ff6b6b','#ff8c42','#ffd43b','#a9e34b','#4ae8a0','#74c0fc','#a78bfa','#f472b6','#94a3b8','#ffffff'];
const DEFAULT_TIERS_TEMPLATE = [
  {id: null, label: 'шедевр', color: '#ff6b6b'},
  {id: null, label: 'A', color: '#ffa94d'},
  {id: null, label: 'B', color: '#ffd43b'},
  {id: null, label: 'C', color: '#74c0fc'},
];

// Обобщённый редактор тиров: рисует список в любой контейнер по id,
// храня данные в переданном массиве (мутирует его прямо по ссылке) и
// вызывая onRemove(idx) при удалении строки (чтобы вызывающий мог сам
// решить, как перерисовать после удаления — массив тиров у разных
// экранов разный: newSessionTiers при создании, sessionAdminTiers в
// настройках существующей сессии).
function renderTierEditorGeneric(containerId, tiersArray, onLabelChange, onRemove) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = tiersArray.map((t, i) => `
    <div class="tier-edit-row" data-idx="${i}">
      <div class="tier-color-pick">
        <div class="tier-color-swatch" style="background:${t.color}" onclick="toggleGenericPalette('${containerId}',${i})"></div>
        <div class="tier-palette" id="palette-${containerId}-${i}" style="display:none">
          ${PALETTE.map(c => `<div class="pal-dot" style="background:${c}" onclick="pickGenericColor('${containerId}',${i},'${c}')"></div>`).join('')}
          <input type="color" value="${t.color}" oninput="pickGenericColor('${containerId}',${i},this.value)" title="Свой цвет">
        </div>
      </div>
      <input class="tier-edit-input" value="${t.label}" maxlength="30"
        oninput="window.__tierEditorState['${containerId}'][${i}].label=this.value"
        placeholder="Название тира">
      <button class="tier-del-btn" onclick="removeGenericTier('${containerId}',${i})" title="Удалить" ${tiersArray.length<=2?'disabled':''}>✕</button>
      <div class="tier-drag-hint">⠿</div>
    </div>`).join('');

  window.__tierEditorState = window.__tierEditorState || {};
  window.__tierEditorState[containerId] = tiersArray;
  window.__tierEditorRenderers = window.__tierEditorRenderers || {};
  window.__tierEditorRenderers[containerId] = () => renderTierEditorGeneric(containerId, tiersArray, onLabelChange, onRemove);
}
function toggleGenericPalette(containerId, i) {
  document.querySelectorAll(`#${containerId} .tier-palette`).forEach((p, j) => {
    p.style.display = (j===i && p.style.display==='none') ? 'flex' : 'none';
  });
}
function pickGenericColor(containerId, i, color) {
  window.__tierEditorState[containerId][i].color = color;
  window.__tierEditorRenderers[containerId]();
}
function removeGenericTier(containerId, i) {
  const arr = window.__tierEditorState[containerId];
  if (arr.length <= 2) return;
  arr.splice(i, 1);
  window.__tierEditorRenderers[containerId]();
}

// ── Тиры в форме СОЗДАНИЯ сессии (массив newSessionTiers) ──────────────────
function renderNewSessionTierEditor() {
  renderTierEditorGeneric('new-session-tier-editor', newSessionTiers);
}
function addNewSessionTier() {
  if (newSessionTiers.length >= 10) { toast('Максимум 10 тиров', 'err'); return; }
  newSessionTiers.push({id: null, label: 'Новый тир', color: PALETTE[newSessionTiers.length % PALETTE.length]});
  renderNewSessionTierEditor();
}

// ── Тиры в НАСТРОЙКАХ уже существующей сессии (массив sessionAdminTiers) ───
let sessionAdminTiers = [];
function renderSessionTierEditor() {
  sessionAdminTiers = JSON.parse(JSON.stringify(currentSessionInfo?.tiers || tiers));
  renderTierEditorGeneric('session-tier-editor', sessionAdminTiers);
}
function addSessionTier() {
  if (sessionAdminTiers.length >= 10) { toast('Максимум 10 тиров', 'err'); return; }
  sessionAdminTiers.push({id: null, label: 'Новый тир', color: PALETTE[sessionAdminTiers.length % PALETTE.length]});
  renderTierEditorGeneric('session-tier-editor', sessionAdminTiers);
}
async function saveSessionTierLabels() {
  if (!currentSessionId) return;
  try {
    const updated = await req('POST', `/api/sessions/${currentSessionId}/tiers`, {tiers: sessionAdminTiers});
    tiers = updated;
    toast('Тиры сохранены!', 'ok');
    renderSessionTierEditor();
  } catch(e) { toast(e.message, 'err'); }
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
        <td><button class="del-btn" onclick="deletePhoto(${p.id})">✕</button></td>
      </tr>`).join('');
  } catch(e) { toast(e.message,'err'); }
}
async function deletePhoto(id) {
  if (!confirm('Удалить? Фото будет удалено из всех сессий, включая уже идущие.')) return;
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

function textColorFor(hex) {
  const h = hex.replace('#','');
  const r=parseInt(h.slice(0,2),16), g=parseInt(h.slice(2,4),16), b=parseInt(h.slice(4,6),16);
  return (0.299*r+0.587*g+0.114*b)/255 > 0.55 ? '#1a1a1a' : '#ffffff';
}

async function loadTierlist() {
  if (!currentSessionId) return;
  try {
    const data = await req('GET', `/api/sessions/${currentSessionId}/tierlist`);
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
      emptyEl.innerHTML = `<div style="font-size:3rem">⏳</div><p>Пока нет оценённых фотографий.</p>`;
    } else {
      emptyEl.style.display = 'none';
    }
  } catch(e) { toast(e.message,'err'); }
}

function showTierlist() {
  if (!currentSessionId) return;
  clearPolling();
  showScreen('tierlist');
  document.getElementById('tierlist-nav-session-title').textContent = currentSessionInfo?.title || '';
  loadTierlist();
}

// ── GALLERY (просмотр всех фото вне сессий, без оценок/тегов) ───────────────

let galleryTagsCache = [];
let gallerySelectedTagId = null;

function showGallery() {
  clearPolling();
  currentSessionId = null;
  showScreen('gallery');
  document.getElementById('gallery-nav-username').textContent = me?.username || '';
  document.getElementById('btn-publish-tierlist').style.display = me?.is_admin ? '' : 'none';
  gallerySelectedTagId = null;
  document.getElementById('gallery-tag-search').value = '';
  document.getElementById('gallery-show-ai-check').checked = false;
  loadGalleryTags();
  loadGalleryPhotos();
}

function galleryShowAi() {
  return document.getElementById('gallery-show-ai-check').checked;
}

async function loadGalleryPhotos() {
  const grid = document.getElementById('gallery-grid');
  const emptyEl = document.getElementById('gallery-empty');
  grid.innerHTML = '';
  emptyEl.style.display = 'none';
  try {
    const params = new URLSearchParams();
    if (gallerySelectedTagId) params.set('tag_id', gallerySelectedTagId);
    if (galleryShowAi()) params.set('include_suggestions', 'true');
    const photos = await req('GET', `/api/gallery/photos?${params.toString()}`);
    if (!photos.length) {
      emptyEl.style.display = '';
      return;
    }
    grid.innerHTML = photos.map(p => `
      <div class="gallery-photo" onclick="openGalleryPhotoModal(${p.id},'${p.filename}')" title="Нажмите для просмотра">
        <img src="/photos/${p.filename}" alt="" loading="lazy">
      </div>`).join('');
  } catch(e) { toast(e.message, 'err'); }
}

function openGalleryPhotoModal(photoId, filename) {
  // Лёгкая версия модалки без оценок/тегов — галерея вне сессий принципиально
  // не показывает кто и как оценил, только сам снимок крупным планом.
  const modal = document.getElementById('photo-modal');
  const img = document.getElementById('modal-img');
  const fname = document.getElementById('modal-filename');
  const votesEl = document.getElementById('modal-votes');
  const tagsEl = document.getElementById('modal-tags');
  img.src = `/photos/${filename}`;
  fname.textContent = '';
  votesEl.innerHTML = '<div class="modal-loading">Галерея — оценки и теги недоступны вне сессии</div>';
  tagsEl.innerHTML = '';
  modal.style.display = 'flex';
  document.body.style.overflow = 'hidden';
}

async function loadGalleryTags() {
  const list = document.getElementById('gallery-tag-list');
  try {
    galleryTagsCache = await req('GET', `/api/gallery/tags?include_suggestions=${galleryShowAi()}`);
    renderGalleryTagPanel('');
    loadGalleryPhotos();
  } catch {
    list.innerHTML = '<span class="tl-tags-loading">Теги недоступны</span>';
  }
}

function renderGalleryTagPanel(query) {
  const list = document.getElementById('gallery-tag-list');
  const clearBtn = document.getElementById('gallery-filter-clear');
  const hint = document.getElementById('gallery-filter-hint');
  const q = (query || '').trim().toLowerCase();
  const visible = galleryTagsCache.filter(t => !q || t.name.toLowerCase().includes(q));

  if (!galleryTagsCache.length) {
    list.innerHTML = '<span class="tl-tags-loading">Тегов пока нет</span>';
    clearBtn.style.display = 'none';
    hint.textContent = '';
    return;
  }

  clearBtn.style.display = gallerySelectedTagId ? '' : 'none';
  hint.textContent = gallerySelectedTagId
    ? galleryTagsCache.find(t => t.id === gallerySelectedTagId)?.name || ''
    : 'Выберите тег';

  list.innerHTML = visible.map(t => `
    <button class="tl-tag-chip ${t.id === gallerySelectedTagId ? 'active' : ''}"
      onclick="selectGalleryTag(${t.id})">${!t.has_confirmed ? '🤖 ' : ''}${escapeHtml(t.name)} <span style="opacity:.7">${t.photo_count}</span></button>
  `).join('') || '<span class="tl-tags-loading">Ничего не найдено</span>';
}

function selectGalleryTag(tagId) {
  gallerySelectedTagId = gallerySelectedTagId === tagId ? null : tagId;
  renderGalleryTagPanel(document.getElementById('gallery-tag-search').value);
  loadGalleryPhotos();
}

function clearGalleryTagFilter() {
  gallerySelectedTagId = null;
  document.getElementById('gallery-tag-search').value = '';
  renderGalleryTagPanel('');
  loadGalleryPhotos();
}

// ── PUBLISHED TIERLIST (отдельная вкладка) ──────────────────────────────────

function showPublishedTierlist() {
  clearPolling();
  showScreen('published-tierlist');
  document.getElementById('btn-publish-tierlist-2').style.display = me?.is_admin ? '' : 'none';
  loadPublishedTierlist();
}

async function loadPublishedTierlist() {
  const content = document.getElementById('published-tierlist-content');
  content.innerHTML = '<div class="stats-loading">Загрузка...</div>';
  try {
    const data = await req('GET', '/api/gallery/published-tierlist');
    if (!data) {
      content.innerHTML = `
        <div class="empty-state" style="margin-top:2rem">
          <div style="font-size:3rem">🏆</div>
          <p>Пока ничего не опубликовано.</p>
          ${me?.is_admin ? '<p class="sub">Нажмите 📌 в шапке, чтобы выбрать сессию для публикации.</p>' : ''}
        </div>`;
      return;
    }
    const tierMap = data.tier_map || {};
    const rowsHtml = data.tier_order.map(tid => {
      const photos = data.tiers[tid] || [];
      const info = tierMap[tid] || {label: tid, color: '#888'};
      const textColor = textColorFor(info.color);
      return `<div class="published-tier-row">
        <div class="tier-label" style="background:${info.color};color:${textColor}">
          <span class="tier-name-full">${info.label}</span>
        </div>
        <div class="tier-photos">
          ${photos.length
            ? photos.map(p=>`<div class="tier-photo" onclick="openGalleryPhotoModal(${p.id},'${p.filename}')">
                <img src="/photos/${p.filename}" alt="" loading="lazy">
                <div class="score-badge" style="background:${info.color};color:${textColor}">${p.vote_count}✓</div>
              </div>`).join('')
            : '<span class="tier-empty">—</span>'}
        </div>
      </div>`;
    }).join('');

    content.innerHTML = `
      <div class="published-tierlist-block">
        <div class="published-tierlist-header">
          <span class="published-tierlist-title">🏆 ${escapeHtml(data.title || 'Тир-лист')}</span>
          <span class="published-tierlist-meta">опубликовал ${escapeHtml(data.published_by || '?')} · ${new Date(data.published_at + 'Z').toLocaleDateString('ru')}</span>
        </div>
        ${rowsHtml}
      </div>`;
  } catch(e) {
    content.innerHTML = `<div class="empty-state"><p>${e.message}</p></div>`;
  }
}

async function openPublishTierlistModal() {
  const modal = document.getElementById('publish-tierlist-modal');
  const list = document.getElementById('publishable-sessions-list');
  list.innerHTML = '<div class="sessions-empty">Загрузка...</div>';
  modal.style.display = 'flex';
  try {
    const sessions = await req('GET', '/api/admin/publishable-sessions');
    if (!sessions.length) {
      list.innerHTML = '<div class="sessions-empty">Сессий пока нет</div>';
      return;
    }
    list.innerHTML = sessions.map(s => `
      <div class="session-card" onclick="publishTierlist(${s.id})">
        <div class="session-card-main">
          <div class="session-card-title">${escapeHtml(s.title)}</div>
          <div class="session-card-meta">от ${escapeHtml(s.creator_username)} · оценено фото: ${s.rated_photo_count} · голосов: ${s.vote_count}</div>
        </div>
        <span class="session-card-badge ${s.is_active ? 'open' : 'closed'}">${s.is_active ? 'активна' : 'завершена'}</span>
      </div>`).join('');
  } catch (e) {
    list.innerHTML = `<div class="sessions-empty">${e.message}</div>`;
  }
}

async function publishTierlist(sessionId) {
  try {
    await req('POST', `/api/admin/publish-tierlist/${sessionId}`);
    document.getElementById('publish-tierlist-modal').style.display = 'none';
    toast('Тир-лист опубликован!', 'ok');
    loadPublishedTierlist();
  } catch (e) { toast(e.message, 'err'); }
}

// ── EXPORT ────────────────────────────────────────────────────────────────────

function getExportFilename(ext) {
  const title = (currentSessionInfo?.title || 'tierlist')
    .replace(/[^a-zа-яёА-ЯЁA-Z0-9_-]/gi, '_').slice(0, 40);
  return `${title}.${ext}`;
}

async function exportCSV() {
  if (!tierlistData) { toast('Сначала загрузите тир-лист','err'); return; }
  const tierMap = tierlistData.tier_map || {};
  let csv = `# Сессия: ${currentSessionInfo?.title || ''}\n`;
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

  const tagLabel = currentSessionInfo?.title || '';

  const maxPhotosPerRow = Math.max(...rows.map(r=>r.photos.length));
  const rowH = THUMB + PAD*2;
  const canvasW = LABEL_W + Math.min(maxPhotosPerRow, 12) * (THUMB+GAP) + PAD*2 + 40;
  const titleLines = tagLabel ? 2 : 1;
  const canvasH = rows.length * (rowH + GAP) + 52 + titleLines * 24;

  const canvas = document.createElement('canvas');
  canvas.width = canvasW; canvas.height = canvasH;
  const ctx = canvas.getContext('2d');

  ctx.fillStyle = '#f5f5f7';
  ctx.fillRect(0, 0, canvasW, canvasH);

  ctx.fillStyle = '#1d1d1f';
  ctx.font = 'bold 20px sans-serif';
  ctx.fillText('Тир-лист результатов', PAD, 30);
  if (tagLabel) {
    ctx.fillStyle = '#0071e3';
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
  if (!currentPhotoId || !currentSessionId) return;
  const list = document.getElementById('votes-list');
  list.innerHTML = '<div class="votes-loading">Загрузка...</div>';
  try {
    const votes = await req('GET', `/api/sessions/${currentSessionId}/photo-votes/${currentPhotoId}`);
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
  if (!currentSessionId) return;
  clearPolling();
  showScreen('stats');
  document.getElementById('stats-nav-session-title').textContent = currentSessionInfo?.title || '';
  selectedUserId = null; compareUserId = null;
  document.getElementById('stats-content').innerHTML = `
    <div class="stats-placeholder">
      <div style="font-size:3rem">👆</div>
      <p>Выберите участника чтобы увидеть статистику</p>
    </div>`;
  loadUsersList();
}

async function loadUsersList() {
  if (!currentSessionId) return;
  try {
    usersList = await req('GET', `/api/sessions/${currentSessionId}/users-list`);
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
  if (!currentSessionId) return;
  const el = document.getElementById('stats-content');
  el.innerHTML = '<div class="stats-loading">Загрузка...</div>';
  try {
    const d = await req('GET', `/api/sessions/${currentSessionId}/user-stats/${uid}`);
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
  if (!currentSessionId) return;
  compareUserId = uid2;
  // re-render compare buttons
  document.querySelectorAll('.compare-btn').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.trim() === usersList.find(u => u.id === uid2)?.username);
  });
  const el = document.getElementById('compare-result');
  if (!el) return;
  el.innerHTML = '<div class="stats-loading" style="margin-top:10px">Загрузка...</div>';
  try {
    const d = await req('GET', `/api/sessions/${currentSessionId}/compare/${uid1}/${uid2}`);
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
  if (!currentSessionId) return;
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
    const d = await req('GET', `/api/sessions/${currentSessionId}/photo-detail/${photoId}`);
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

  // мои подтверждённые теги — съёмные чипы
  const myHtml = myTags.map(t => `
    <span class="tag-chip mine" title="Нажмите чтобы удалить">
      ${t.name}
      <span class="tag-remove" onclick="removeTag(${t.id})">×</span>
    </span>`).join('');

  // подтверждённые теги других пользователей, сгруппированные по имени
  const myTagNames = new Set(myTags.map(t => t.name));
  const confirmed = allPhotoTags.filter(r => !r.is_suggestion);
  const others = {};
  confirmed.forEach(r => {
    if (!others[r.tag_name]) others[r.tag_name] = [];
    others[r.tag_name].push(r.username);
  });
  const othersHtml = Object.entries(others)
    .filter(([name]) => !myTagNames.has(name))
    .map(([name, users]) => `
      <span class="tag-chip other" title="${users.join(', ')}: ${name}"
            onclick="addTagByName('${name.replace(/'/g,"\'")}')">
        ${name} <span class="tag-users">${users.length}</span>
      </span>`).join('');

  // предложения от автотегирования — отдельная группа с кнопками подтвердить/отклонить.
  // Если тег уже подтверждён (есть среди myTags/others), повторно как suggestion не показываем —
  // это та же самая запись в БД, просто её is_suggestion уже снят.
  const suggestions = allPhotoTags.filter(r => r.is_suggestion);
  const suggestionsHtml = suggestions.map(r => `
      <span class="tag-chip suggestion" title="Предложено автотегированием — подтвердите или отклоните">
        <span class="ai-badge">AI</span> ${r.tag_name}
        <span class="tag-confirm" onclick="confirmSuggestedTag(${r.tag_id})" title="Подтвердить">✓</span>
        <span class="tag-reject" onclick="rejectSuggestedTag(${r.tag_id})" title="Отклонить">✕</span>
      </span>`).join('');

  sel.innerHTML = myHtml + othersHtml + suggestionsHtml;
}

async function confirmSuggestedTag(tagId) {
  try {
    await formReq(`/api/photo-tags/${currentPhotoId}/confirm`, { tag_id: tagId });
    loadTags();
  } catch(e) { toast(e.message || 'Ошибка', 'err'); }
}

async function rejectSuggestedTag(tagId) {
  try {
    await formReq(`/api/photo-tags/${currentPhotoId}/reject`, { tag_id: tagId });
    loadTags();
  } catch(e) { toast(e.message || 'Ошибка', 'err'); }
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

function formatBytes(n) {
  if (!n) return '0';
  if (n < 1024) return `${n} Б`;
  if (n < 1024*1024) return `${(n/1024).toFixed(0)} КБ`;
  return `${(n/1024/1024).toFixed(1)} МБ`;
}

async function loadWd14Status() {
  const el = document.getElementById('wd14-status-content');
  if (!el) return;
  try {
    const d = await req('GET', '/api/admin/wd14-status');
    if (d.available) {
      el.innerHTML = `<span class="watch-on">⬤ Модель загружена и работает</span><br>
        <span class="watch-meta">model.onnx: ${formatBytes(d.model_size_bytes)} · selected_tags.csv: ${formatBytes(d.tags_size_bytes)}</span>`;
    } else {
      // Различаем "файлов вообще нет" и "файлы есть, но это LFS-указатели 0 КБ" —
      // вторая ситуация встречается очень часто при неправильном скачивании с HuggingFace.
      const looksLikePointer = d.model_size_bytes > 0 && d.model_size_bytes < 50*1024*1024;
      const hint = looksLikePointer
        ? `Файл model.onnx весит всего ${formatBytes(d.model_size_bytes)} — это похоже на LFS/Xet-указатель, а не на реальную модель (~388 МБ). Скачайте файл заново по прямой ссылке (.../resolve/main/model.onnx), а не через предпросмотр страницы.`
        : `Файлы model.onnx и selected_tags.csv не найдены в wd14_model/. Автотеги не проставляются, но загрузка фото работает как обычно.`;
      el.innerHTML = `<span class="watch-off">⬤ Модель не подключена</span><br>
        <span class="watch-meta">${hint}</span>`;
    }
  } catch(e) {
    el.textContent = '';
  }
}

async function checkDuplicatePhotos() {
  const statusEl = document.getElementById('duplicate-status');
  const mergeBtn = document.getElementById('merge-duplicates-btn');
  if (!statusEl) return;
  try {
    const d = await req('GET', '/api/admin/duplicate-photos');
    if (d.groups === 0) {
      statusEl.innerHTML = '<span class="watch-on">⬤ Дублей не найдено</span>';
      mergeBtn.style.display = 'none';
    } else {
      statusEl.innerHTML = `<span class="watch-off">⬤ Найдено ${d.groups} групп дублей, лишних фото: ${d.extra_photos}</span>`;
      mergeBtn.style.display = '';
    }
  } catch(e) {
    statusEl.textContent = '';
  }
}

async function mergeDuplicatePhotos() {
  if (!confirm('Объединить дубли? Лишние копии будут удалены, а их голоса и теги перенесены на оставшуюся копию. Действие необратимо.')) return;
  const btn = document.getElementById('merge-duplicates-btn');
  btn.disabled = true; btn.textContent = 'Объединяю...';
  try {
    const d = await req('POST', '/api/admin/duplicate-photos/merge');
    toast(`Объединено групп: ${d.merged_groups}, удалено лишних копий: ${d.removed_photos}`, 'ok');
    checkDuplicatePhotos();
    loadPhotos();
    loadAdminStats();
  } catch(e) {
    toast(e.message || 'Ошибка', 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'Объединить дубли';
  }
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

// ── GOOGLE DRIVE IMPORT & AUTO-SYNC ─────────────────────────────────────────
// Полный аналог блока Яндекс.Диска выше, только источник — публичная папка
// Google Drive (доступ «у кого есть ссылка»). Подпапки сканируются
// автоматически и сворачиваются в общий плоский список фото.

async function previewGdrive() {
  const url = document.getElementById('gdrive-url').value.trim();
  if (!url) { toast('Введите ссылку на папку Google Диска', 'err'); return; }
  const preview = document.getElementById('gdrive-preview');
  const importBtn = document.getElementById('gdrive-import-btn');
  preview.style.display = '';
  preview.textContent = 'Загрузка списка файлов...';
  importBtn.style.display = 'none';
  try {
    const data = await req('GET', '/api/admin/preview-gdrive?folder_url=' + encodeURIComponent(url));
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

async function importGdrive() {
  const url = document.getElementById('gdrive-url').value.trim();
  if (!url) { toast('Введите ссылку на папку Google Диска', 'err'); return; }
  const progress = document.getElementById('gdrive-progress');
  const label = document.getElementById('gdrive-label');
  const fill = document.getElementById('gdrive-fill');
  const importBtn = document.getElementById('gdrive-import-btn');
  importBtn.style.display = 'none';
  progress.style.display = '';
  label.textContent = 'Идёт импорт, подождите...';
  fill.style.width = '100%';
  try {
    const fd = new FormData();
    fd.append('folder_url', url);
    const resp = await fetch(API + '/api/admin/import-gdrive', {
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

async function loadGdriveWatchStatus() {
  try {
    const d = await req('GET', '/api/admin/gdrive-watch');
    const status = document.getElementById('gdrive-watch-status');
    const syncBtn = document.getElementById('gdrive-watch-sync-now-btn');
    const delBtn = document.getElementById('gdrive-watch-delete-btn');
    if (!d) {
      status.innerHTML = '<span class="watch-off">⬤ Выключено</span>';
      syncBtn.style.display = 'none';
      delBtn.style.display = 'none';
      return;
    }
    document.getElementById('gdrive-watch-url').value = d.folder_url || '';
    const sel = document.getElementById('gdrive-watch-interval');
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
    document.getElementById('gdrive-watch-status').textContent = '';
  }
}

async function saveGdriveWatchUrl() {
  const url = document.getElementById('gdrive-watch-url').value.trim();
  if (!url) { toast('Введите ссылку', 'err'); return; }
  const interval = document.getElementById('gdrive-watch-interval').value;
  try {
    await formReq('/api/admin/gdrive-watch', { folder_url: url, interval_minutes: interval });
    toast('Авто-синхронизация включена! Первая проверка запущена.', 'ok');
    loadGdriveWatchStatus();
  } catch(e) { toast(e.message || 'Ошибка', 'err'); }
}

async function deleteGdriveWatchUrl() {
  if (!confirm('Отключить авто-синхронизацию?')) return;
  try {
    await req('DELETE', '/api/admin/gdrive-watch');
    toast('Авто-синхронизация отключена', 'ok');
    document.getElementById('gdrive-watch-url').value = '';
    loadGdriveWatchStatus();
  } catch(e) { toast(e.message, 'err'); }
}

async function gdriveWatchSyncNow() {
  const btn = document.getElementById('gdrive-watch-sync-now-btn');
  btn.disabled = true; btn.textContent = '↻ Проверяю...';
  try {
    const d = await req('POST', '/api/admin/gdrive-watch/sync-now');
    toast(`Готово: добавлено ${d.added}, пропущено ${d.skipped}` + (d.errors ? `, ошибок ${d.errors}` : ''), 'ok');
    loadGdriveWatchStatus();
    if (d.added > 0) loadPhotos();
  } catch(e) { toast(e.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '↻ Проверить сейчас'; }
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
