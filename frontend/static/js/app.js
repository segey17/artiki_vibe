const API = '';
let token = localStorage.getItem('token') || null;
let me = null;
let currentPhotoId = null;
let selectedTierId = null;
let tiers = [];
let pollTimer = null;

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
}
function showAdmin() { clearPolling(); showScreen('admin'); loadPhotos(); loadAdminStats(); renderTierEditor(); }
function showTierlist() { clearPolling(); showScreen('tierlist'); loadTierlist(); }

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

function startPolling() { clearPolling(); pollTimer = setInterval(loadCurrentPhoto, 5000); }
function clearPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

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
async function loadTierlist() {
  try {
    const data = await req('GET','/api/tierlist');
    const tierMap = data.tier_map || {};
    const container = document.getElementById('tierlist-container');
    let hasAny = false;
    container.innerHTML = data.tier_order.map(tid => {
      const photos = data.tiers[tid] || [];
      const info = tierMap[tid] || {label: tid, color: '#888'};
      if (photos.length) hasAny = true;
      // text color: luminance-based
      const hex = info.color.replace('#','');
      const r=parseInt(hex.slice(0,2),16), g=parseInt(hex.slice(2,4),16), b=parseInt(hex.slice(4,6),16);
      const lum = (0.299*r+0.587*g+0.114*b)/255;
      const textColor = lum > 0.55 ? '#1a1a1a' : '#ffffff';
      return `<div class="tier-row">
        <div class="tier-label" style="background:${info.color};color:${textColor}">
          <span class="tier-name-full">${info.label}</span>
        </div>
        <div class="tier-photos">
          ${photos.length
            ? photos.map(p=>`<div class="tier-photo" title="${p.original_name||p.filename} (${p.vote_count} гол.)">
                <img src="/photos/${p.filename}" alt="" loading="lazy">
                <div class="score-badge" style="background:${info.color};color:${textColor}">${p.vote_count}✓</div>
              </div>`).join('')
            : '<span class="tier-empty">—</span>'}
        </div>
      </div>`;
    }).join('');
    document.getElementById('tierlist-empty').style.display = hasAny ? 'none' : '';
  } catch(e) { toast(e.message,'err'); }
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
