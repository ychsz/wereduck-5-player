'use strict';

// ===== Global State =====
const G = {
  ws: null,
  roomCode: null,
  mySeat: null,
  secret: null,
  state: null,
  roles: [],
  rolesById: {},
  selectedSeat: null,    // for seat selection screen
  marks: {},             // {targetSeat: roleId}
  nightTarget: null,     // selected night action target
  nightExtra: {},        // extra fields for complex actions
  voteTarget: null,
  countdownTimer: null,
  prevPhase: null,       // track phase transitions for clearing marks
  prevDayCount: 0,
};

// ===== Init =====
window.addEventListener('DOMContentLoaded', init);

async function init() {
  // Load roles for search + mark
  try {
    const res = await fetch('/api/roles');
    G.roles = await res.json();
    G.rolesById = {};
    for (const r of G.roles) G.rolesById[r.id] = r;
  } catch (e) { console.error('Failed to load roles', e); }

  // Landing buttons
  document.getElementById('btn-create').onclick = onCreate;
  document.getElementById('btn-join').onclick = onJoin;
  document.getElementById('input-join-code').addEventListener('input', e => {
    e.target.value = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
  });
  document.getElementById('input-join-code').addEventListener('keydown', e => { if (e.key === 'Enter') onJoin(); });

  // Seat select
  document.getElementById('btn-sit').onclick = onSit;

  // Chat
  document.getElementById('btn-send').onclick = sendChat;
  document.getElementById('chat-input').addEventListener('keydown', e => { if (e.key === 'Enter') sendChat(); });

  // Search
  document.getElementById('btn-search').onclick = openSearch;
  document.getElementById('btn-close-search').onclick = closeSearch;
  document.getElementById('search-input').addEventListener('input', renderSearch);

  // Mark
  document.getElementById('btn-mark-close').onclick = () => document.getElementById('mark-dialog').classList.add('hidden');
  document.getElementById('btn-mark-clear').onclick = clearMark;
  document.getElementById('mark-search').addEventListener('input', renderMarkList);

  // Guess
  document.getElementById('btn-guess-close').onclick = () => document.getElementById('guess-dialog').classList.add('hidden');
  document.getElementById('btn-guess-submit').onclick = submitGuess;
  document.getElementById('guess-role-search').addEventListener('input', renderGuessRoleList);

  // Ready
  document.getElementById('btn-ready').onclick = () => send({ type: 'ready' });
  document.getElementById('btn-ready-again').onclick = () => {
    const go = G.state && G.state.game_over;
    const me = go && go.seats && go.seats.find(s => s.seat === G.mySeat);
    if (me && me.ready) {
      send({ type: 'cancel_ready' });
    } else {
      send({ type: 'ready' });
    }
  };
}

// ===== Landing =====
async function onCreate() {
  try {
    const res = await fetch('/api/room', { method: 'POST' });
    const data = await res.json();
    showSeatSelect(data.code);
  } catch (e) {
    document.getElementById('landing-error').textContent = '创建房间失败';
  }
}

function onJoin() {
  const code = document.getElementById('input-join-code').value.trim().toUpperCase();
  if (code.length !== 4) {
    document.getElementById('landing-error').textContent = '请输入4位房间号';
    return;
  }
  document.getElementById('landing-error').textContent = '';
  showSeatSelect(code);
}

// ===== Seat Selection =====
async function showSeatSelect(code) {
  G.roomCode = code;
  document.getElementById('landing').classList.add('hidden');
  document.getElementById('seat-select').classList.remove('hidden');
  document.getElementById('room-code-display').textContent = code;
  G.selectedSeat = null;
  await refreshSeatSelect();
}

async function refreshSeatSelect() {
  try {
    const res = await fetch(`/api/room/${G.roomCode}`);
    if (!res.ok) { document.getElementById('seat-error').textContent = '房间不存在'; return; }
    const info = await res.json();
    const grid = document.getElementById('seat-grid-select');
    grid.innerHTML = '';
    for (const s of info.seats) {
      const btn = document.createElement('div');
      btn.className = 'seat-btn' + (s.taken ? ' taken' : ' empty') + (s.seat === G.selectedSeat ? ' selected' : '');
      btn.innerHTML = `<span class="seat-num">${s.seat}</span><span class="seat-name">${s.name || (s.taken ? '已占' : '空')}</span>`;
      if (!s.taken) btn.onclick = () => { G.selectedSeat = s.seat; refreshSeatSelect(); };
      grid.appendChild(btn);
    }
  } catch (e) {
    document.getElementById('seat-error').textContent = '获取房间信息失败';
  }
}

function onSit() {
  const name = document.getElementById('input-name').value.trim();
  if (!G.selectedSeat) { document.getElementById('seat-error').textContent = '请选择一个座位'; return; }
  if (!name) { document.getElementById('seat-error').textContent = '请输入昵称'; return; }
  document.getElementById('seat-error').textContent = '';
  connectWS(G.roomCode, G.selectedSeat, name, '');
}

// ===== WebSocket =====
function connectWS(code, seat, name, secret) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/ws/${code}`;
  G.ws = new WebSocket(url);

  G.ws.onopen = () => {
    send({ type: 'join', seat: parseInt(seat), name, secret });
  };
  G.ws.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    handleMessage(data);
  };
  G.ws.onclose = () => {
    if (G.state && G.state.phase !== 'game_over') {
      // auto-reconnect
      setTimeout(() => {
        if (G.secret) connectWS(G.roomCode, G.mySeat, '', G.secret);
      }, 2000);
    }
  };
  G.ws.onerror = () => {};
}

function send(obj) {
  if (G.ws && G.ws.readyState === 1) G.ws.send(JSON.stringify(obj));
}

function handleMessage(data) {
  if (data.type === 'joined') {
    G.mySeat = data.seat;
    G.secret = data.secret;
    loadMarks();
    document.getElementById('seat-select').classList.add('hidden');
    document.getElementById('landing').classList.add('hidden');
    document.getElementById('game').classList.remove('hidden');
    return;
  }
  if (data.type === 'error') {
    // show errors contextually
    if (data.text) showTransientError(data.text);
    return;
  }
  if (data.type === 'guess_result') {
    handleGuessResult(data);
    return;
  }
  if (data.type === 'state') {
    G.state = data;
    delete G.state.type;
    render();
  }
}

function showTransientError(text) {
  // try guess result field first
  const gr = document.getElementById('guess-result');
  if (gr && !document.getElementById('guess-dialog').classList.contains('hidden')) {
    gr.textContent = text;
    return;
  }
  const se = document.getElementById('seat-error');
  if (se && !document.getElementById('seat-select').classList.contains('hidden')) {
    se.textContent = text;
    return;
  }
  // fallback: brief flash in chat
  const ca = document.getElementById('chat-area');
  if (ca) {
    const d = document.createElement('div');
    d.className = 'chat-msg system';
    d.textContent = '⚠️ ' + text;
    ca.appendChild(d);
    ca.scrollTop = ca.scrollHeight;
  }
}

// ===== Marks (localStorage) =====
function loadMarks() {
  try {
    const raw = localStorage.getItem(`wereduck_marks_${G.roomCode}_${G.mySeat}`);
    G.marks = raw ? JSON.parse(raw) : {};
  } catch { G.marks = {}; }
}

function saveMarks() {
  try {
    localStorage.setItem(`wereduck_marks_${G.roomCode}_${G.mySeat}`, JSON.stringify(G.marks));
  } catch {}
}

// ===== Render =====
function render() {
  if (!G.state) return;
  // Detect new game starting: phase transitions to night with day_count == 1
  // from a non-night phase (game_over, waiting, ready), or day_count resets.
  const newPhase = G.state.phase;
  const newDay = G.state.day_count;
  if (newPhase === 'night' && newDay === 1
      && G.prevPhase !== null && G.prevPhase !== 'night') {
    G.marks = {};
    saveMarks();
    renderSeats();
  }
  G.prevPhase = newPhase;
  G.prevDayCount = newDay;
  renderHeader();
  renderRoleCard();
  renderSeats();
  renderNightPanel();
  renderSpeechPanel();
  renderVotePanel();
  renderChat();
  renderOverlays();
}

function renderHeader() {
  const phaseMap = { waiting: '等候中', ready: '准备中', night: '🌙 夜晚', day_speech: '☀️ 白天·轮麦', day_vote: '🗳️ 白天·投票', game_over: '游戏结束' };
  document.getElementById('phase-label').textContent = phaseMap[G.state.phase] || G.state.phase;
  document.getElementById('day-label').textContent = G.state.day_count > 0 ? `第${G.state.day_count}天` : '';
  document.getElementById('room-label').textContent = G.roomCode;
}

function renderRoleCard() {
  const card = document.getElementById('role-card');
  if (!G.state.my_role) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  const r = G.state.my_role;
  // "X号 昵称" label at top of card
  const mySeat = G.state.my_seat;
  const myName = (G.state.seats.find(s => s.seat === mySeat) || {}).name || '';
  document.getElementById('role-id-label').textContent = `${mySeat}号 ${myName}`;
  document.getElementById('role-name').textContent = r.name;
  const f = document.getElementById('role-faction');
  f.textContent = r.faction_name;
  f.className = 'role-faction ' + r.faction;
  document.getElementById('role-desc').textContent = r.description;
  // private notes
  const pn = document.getElementById('private-notes');
  const notes = G.state.private || {};
  const lines = [];
  if (notes.belly) lines.push(`肚中：${notes.belly.join('、')}`);
  if (notes.in_belly_of) lines.push(`你被 ${notes.in_belly_of} 吞食`);
  if (notes.infected) lines.push(`已感染：${notes.infected.join('、')}`);
  if (notes.lover) lines.push(`恋人：${notes.lover}`);
  if (notes.vulture_count !== undefined) lines.push(`秃鹫计数：${notes.vulture_count}/2`);
  if (notes.magpie_correct_count !== undefined) lines.push(`喜鹊猜对：${notes.magpie_correct_count}/2`);
  if (notes.priest_double) lines.push(`翻倍目标：${notes.priest_double}`);
  if (notes.lobbyist_charges !== undefined) lines.push(`说客杀人机会：${notes.lobbyist_charges}`);
  if (notes.spy_intel) for (const [seat, role] of Object.entries(notes.spy_intel)) lines.push(`间谍情报：${seat}号=${role}`);
  if (notes.intel) for (const m of notes.intel) lines.push(m);
  pn.textContent = lines.join('\n');
}

function toggleRoleCard() {
  document.getElementById('role-desc').classList.toggle('hidden');
  const icon = document.getElementById('role-expand-icon');
  icon.style.transform = icon.style.transform === 'rotate(180deg)' ? '' : 'rotate(180deg)';
}
window.toggleRoleCard = toggleRoleCard;

function renderSeats() {
  const grid = document.getElementById('seat-grid-game');
  grid.innerHTML = '';
  const seats = G.state.seats || [];
  const speaker = (G.state.day_speech && G.state.day_speech.speaker_seat) || null;
  for (const s of seats) {
    const cell = document.createElement('div');
    cell.className = 'seat-cell';
    if (s.status === 'empty') { cell.classList.add('empty-placeholder'); cell.style.opacity = '.25'; }
    if (s.status === 'dead') cell.classList.add('dead');
    if (s.seat === G.mySeat) cell.classList.add('me');
    if (s.seat === speaker) cell.classList.add('speaking');
    const mark = G.marks[String(s.seat)];
    cell.innerHTML = `
      <span class="seat-num">${s.seat}</span>
      <span class="seat-name">${s.name || ''}</span>
      ${mark ? `<span class="seat-mark">${G.rolesById[mark] ? G.rolesById[mark].name : mark}</span>` : ''}
      ${!s.online && s.name ? '<span class="seat-offline">离线</span>' : ''}
    `;
    cell.dataset.seat = s.seat;
    grid.appendChild(cell);
  }
}

function onSeatGridClick(e) {
  const cell = e.target.closest('.seat-cell');
  if (!cell || !cell.dataset.seat) return;
  const seat = parseInt(cell.dataset.seat);
  if (seat === G.mySeat) return;
  if (!G.state.seats.find(s => s.seat === seat && s.name)) return;
  openMarkDialog(seat);
}
window.onSeatGridClick = onSeatGridClick;

// ===== Night Action Panel =====
function renderNightPanel() {
  const panel = document.getElementById('night-panel');
  const night = G.state.night;
  if (G.state.phase !== 'night' || !night || !night.is_my_turn) {
    panel.classList.add('hidden');
    if (G.state.phase === 'night') {
      panel.classList.remove('hidden');
      panel.innerHTML = '<div class="ap-text">🌙 夜晚进行中，等待行动...</div>';
    }
    return;
  }
  panel.classList.remove('hidden');
  const prompt = night.prompt;
  G.nightTarget = null;
  G.nightExtra = {};
  let html = `<h3>🌙 ${prompt.role} 行动</h3><div class="ap-text">${prompt.text}</div>`;

  // Status board: show all players' perceived status
  if (prompt.status_board && prompt.status_board.length) {
    html += '<div class="status-board">';
    for (const s of prompt.status_board) {
      const cls = s.status === '存活' ? 'sb-alive' : 'sb-dead';
      html += `<span class="sb-item ${cls}">${s.seat}号${s.name}·${s.status}</span>`;
    }
    html += '</div>';
  }

  // Pipe option (engineer / duck_pipe / pigeon)
  if (prompt.can_pipe && prompt.step === 'pigeon') {
    html += '<div class="target-list">';
    html += `<div class="target-item" data-action="pipe"><input type="radio" name="np"> 钻进管道</div>`;
    for (const t of (prompt.infect_targets || [])) {
      html += `<div class="target-item" data-action="infect" data-seat="${t.seat}"><input type="radio" name="np"> 感染 ${t.name}（${t.seat}号）</div>`;
    }
    html += '</div>';
    html += '<div class="btn-row"><button class="btn btn-primary" id="btn-night-submit">确认</button></div>';
  } else if (prompt.can_pipe) {
    html += '<div class="target-list">';
    html += `<div class="target-item" data-action="pipe"><input type="radio" name="np"> 钻进管道</div>`;
    html += `<div class="target-item" data-action="nopipe"><input type="radio" name="np"> 不钻管道</div>`;
    html += '</div>';
    html += '<div class="btn-row"><button class="btn btn-primary" id="btn-night-submit">确认</button></div>';
  } else if (prompt.role === '丘比特') {
    // kill targets + arrow option
    html += '<div class="target-list">';
    if (prompt.can_arrow) html += `<div class="target-item" data-action="arrow"><input type="radio" name="np"> 使用射箭（连2人为恋人）</div>`;
    for (const t of (prompt.targets || [])) {
      html += `<div class="target-item" data-action="kill" data-seat="${t.seat}"><input type="radio" name="np"> 杀 ${t.name}（${t.seat}号）</div>`;
    }
    if (prompt.can_skip) html += `<div class="target-item" data-action="skip"><input type="radio" name="np"> 放弃</div>`;
    html += '</div>';
    html += '<div id="cupid-arrow-opts" class="hidden"><div class="ap-text">选择2名玩家结为恋人：</div>';
    html += '<select id="cupid-a" class="select">';
    for (const t of (prompt.arrow_targets || [])) html += `<option value="${t.seat}">${t.name}（${t.seat}号）</option>`;
    html += '</select><select id="cupid-b" class="select">';
    for (const t of (prompt.arrow_targets || [])) html += `<option value="${t.seat}">${t.name}（${t.seat}号）</option>`;
    html += '</select></div>';
    html += '<div class="btn-row"><button class="btn btn-primary" id="btn-night-submit">确认</button></div>';
  } else if (prompt.role === '牧师') {
    // kill targets + double target
    html += '<div class="target-list">';
    for (const t of (prompt.targets || [])) {
      html += `<div class="target-item" data-action="kill" data-seat="${t.seat}"><input type="radio" name="np"> 杀 ${t.name}（${t.seat}号）</div>`;
    }
    html += `<div class="target-item" data-action="skip"><input type="radio" name="np"> 不杀人</div>`;
    html += '</div>';
    html += '<div class="ap-text">翻倍投票权（可不给）：</div><select id="priest-double" class="select"><option value="">不翻倍</option>';
    for (const t of (prompt.double_targets || [])) html += `<option value="${t.seat}">${t.name}（${t.seat}号）</option>`;
    html += '</select>';
    html += '<div class="btn-row"><button class="btn btn-primary" id="btn-night-submit">确认</button></div>';
  } else {
    // standard kill or select target
    html += '<div class="target-list">';
    for (const t of (prompt.targets || [])) {
      html += `<div class="target-item" data-action="target" data-seat="${t.seat}"><input type="radio" name="np"> ${t.name}（${t.seat}号）</div>`;
    }
    if (prompt.can_skip) html += `<div class="target-item" data-action="skip"><input type="radio" name="np"> 放弃</div>`;
    // mortician dead targets
    for (const t of (prompt.dead_seats || [])) {
      html += `<div class="target-item" data-action="target" data-seat="${t.seat}"><input type="radio" name="np"> 查看 ${t.name}（${t.seat}号·出局）</div>`;
    }
    html += '</div>';
    if (prompt.targets && prompt.targets.length > 0 || (prompt.dead_seats && prompt.dead_seats.length > 0) || prompt.can_skip) {
      html += '<div class="btn-row"><button class="btn btn-primary" id="btn-night-submit">确认</button></div>';
    } else {
      html += '<div class="ap-text">（无可选目标，跳过）</div>';
      html += '<div class="btn-row"><button class="btn btn-primary" id="btn-night-submit">跳过</button></div>';
    }
  }
  panel.innerHTML = html;

  // wire up
  panel.querySelectorAll('.target-item').forEach(item => {
    item.onclick = () => {
      panel.querySelectorAll('.target-item').forEach(i => { i.classList.remove('selected'); i.querySelector('input').checked = false; });
      item.classList.add('selected');
      item.querySelector('input').checked = true;
      const action = item.dataset.action;
      G.nightExtra.action = action;
      G.nightTarget = (action === 'target' || action === 'kill' || action === 'infect') ? parseInt(item.dataset.seat) : null;
      // show arrow opts
      const arrowOpts = document.getElementById('cupid-arrow-opts');
      if (arrowOpts) arrowOpts.classList.toggle('hidden', action !== 'arrow');
    };
  });
  const submitBtn = document.getElementById('btn-night-submit');
  if (submitBtn) submitBtn.onclick = submitNightAction;
}

function submitNightAction() {
  const prompt = G.state.night.prompt;
  const action = G.nightExtra.action;
  if (!action) {
    // pigeon: must choose
    if (prompt.can_pipe && prompt.step === 'pigeon') return;
    // pipe-only types: default to nopipe? no, they must choose
    return;
  }
  const payload = {};
  if (action === 'skip') payload.skip = true;
  else if (action === 'pipe') { payload.pipe = true; if (prompt.step === 'pigeon') payload.pipe = true; }
  else if (action === 'nopipe') { payload.pipe = false; }
  else if (action === 'infect') { payload.pipe = false; payload.target = G.nightTarget; }
  else if (action === 'target' || action === 'kill') {
    payload.target = G.nightTarget;
    payload.skip = false;
  } else if (action === 'arrow') {
    const a = parseInt(document.getElementById('cupid-a').value);
    const b = parseInt(document.getElementById('cupid-b').value);
    if (a === b) { showTransientError('恋人不能是同一人'); return; }
    payload.use_arrow = true;
    payload.arrow_a = a;
    payload.arrow_b = b;
    payload.skip = false;
  }
  // priest double
  const dbl = document.getElementById('priest-double');
  if (dbl && dbl.value) payload.double_target = parseInt(dbl.value);
  send({ type: 'night_action', action: payload });
  G.nightExtra = {};
  G.nightTarget = null;
}

// ===== Speech Panel =====
function renderSpeechPanel() {
  const panel = document.getElementById('speech-panel');
  if (G.state.phase !== 'day_speech') { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  const ds = G.state.day_speech;
  const myTurn = ds.is_my_turn;
  const speaker = ds.speaker_seat;
  const speakerName = speaker ? (G.state.seats.find(s => s.seat === speaker) || {}).name : '';
  let html = '';
  if (myTurn) {
    html += `<div class="ap-text">🎤 轮到你发言（${speaker}号）。发言完毕后点击结束。</div>`;
    html += '<div class="btn-row"><button class="btn btn-primary" id="btn-end-speech">结束发言</button></div>';
  } else if (speaker) {
    html += `<div class="ap-text">${speakerName}（${speaker}号）正在发言，请仔细聆听。</div>`;
  }
  // guess buttons
  if (G.state.my_alive) {
    if (ds.can_assassinate) html += '<div class="btn-row"><button class="btn-small" id="btn-guess-assassin">🎯 猜身份（刺客）</button></div>';
    if (ds.can_raven_guess) html += `<div class="btn-row"><button class="btn-small" id="btn-guess-raven">🎯 猜目标·${ds.raven_target}（渡鸦）</button></div>`;
    if (ds.can_magpie_guess) html += '<div class="btn-row"><button class="btn-small" id="btn-guess-magpie">🔍 猜身份（喜鹊）</button></div>';
  }
  panel.innerHTML = html;
  const endBtn = document.getElementById('btn-end-speech');
  if (endBtn) endBtn.onclick = () => send({ type: 'end_speech' });
  const ga = document.getElementById('btn-guess-assassin');
  if (ga) ga.onclick = () => openGuessDialog('assassin');
  const gr = document.getElementById('btn-guess-raven');
  if (gr) gr.onclick = () => openGuessDialog('raven');
  const gm = document.getElementById('btn-guess-magpie');
  if (gm) gm.onclick = () => openGuessDialog('magpie');
}

// ===== Vote Panel =====
function renderVotePanel() {
  const panel = document.getElementById('vote-panel');
  if (G.state.phase !== 'day_vote') { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  const dv = G.state.day_vote;
  if (dv.has_voted) {
    panel.innerHTML = `<div class="ap-text">✅ 已投票（仅自己可见）。等待其他玩家投票...</div>`;
    return;
  }
  if (!dv.can_vote) {
    panel.innerHTML = '<div class="ap-text">🗳️ 投票进行中...（你无法投票）</div>';
    return;
  }
  let html = '<h3>🗳️ 投票</h3><div class="target-list">';
  for (const t of dv.targets) {
    const sel = dv.my_vote === t.seat ? 'selected' : '';
    html += `<div class="target-item ${sel}" data-seat="${t.seat}"><input type="radio" name="vp" ${dv.my_vote === t.seat ? 'checked' : ''}> ${t.name}</div>`;
  }
  html += '</div><div class="btn-row"><button class="btn btn-primary" id="btn-vote-submit">投票</button></div>';
  panel.innerHTML = html;
  panel.querySelectorAll('.target-item').forEach(item => {
    item.onclick = () => {
      panel.querySelectorAll('.target-item').forEach(i => { i.classList.remove('selected'); i.querySelector('input').checked = false; });
      item.classList.add('selected');
      item.querySelector('input').checked = true;
      G.voteTarget = parseInt(item.dataset.seat);
    };
  });
  document.getElementById('btn-vote-submit').onclick = () => {
    if (G.voteTarget !== null) send({ type: 'vote', target: G.voteTarget });
  };
}

// ===== Chat =====
function renderChat() {
  const area = document.getElementById('chat-area');
  const msgs = G.state.chat || [];
  // only re-render if changed
  const sig = msgs.map(m => m.text + m.name + m.seat + (m.personal ? '1' : '0')).join('|');
  if (area._sig === sig) { scrollChat(); return; }
  area._sig = sig;
  area.innerHTML = '';
  for (const m of msgs) {
    const d = document.createElement('div');
    let cls = 'chat-msg';
    if (m.system) cls += ' system';
    if (m.personal) cls += ' personal';
    d.className = cls;
    if (m.personal) {
      d.textContent = '🔒 ' + m.text + '（仅自己可见）';
    } else if (m.system) {
      d.textContent = m.text;
    } else {
      d.innerHTML = `<span class="chat-seat">${m.seat}号</span> <span class="chat-name">${m.name}:</span> ${escapeHtml(m.text)}`;
    }
    area.appendChild(d);
  }
  scrollChat();
  // enable/disable chat input
  const input = document.getElementById('chat-input');
  const sendBtn = document.getElementById('btn-send');
  let canChat = false;
  if (G.state.phase === 'day_speech' && G.state.day_speech && G.state.day_speech.is_my_turn) canChat = true;
  if (G.state.phase === 'day_vote' && G.state.my_alive) canChat = true;
  input.disabled = !canChat;
  sendBtn.disabled = !canChat;
  input.placeholder = canChat ? '输入发言...' : '当前阶段不能发言';
}

function scrollChat() {
  const area = document.getElementById('chat-area');
  area.scrollTop = area.scrollHeight;
}

function sendChat() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  send({ type: 'speech', text });
  input.value = '';
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ===== Overlays =====
function renderOverlays() {
  // Ready overlay (waiting/ready phase)
  const readyOv = document.getElementById('ready-overlay');
  if (G.state.phase === 'waiting' || G.state.phase === 'ready') {
    readyOv.classList.remove('hidden');
    const seats = G.state.seats || [];
    const seated = seats.filter(s => s.name);
    const readyCount = seated.filter(s => s.ready).length;
    const me = seats.find(s => s.seat === G.mySeat);
    document.getElementById('ready-status').textContent =
      `房间 ${G.roomCode} · ${seated.length}/5人 · ${readyCount}人已准备`;
    const btn = document.getElementById('btn-ready');
    if (me && me.ready) {
      btn.textContent = '已准备（等待其他玩家）';
      btn.disabled = true;
    } else {
      btn.textContent = '准备';
      btn.disabled = false;
    }
  } else {
    readyOv.classList.add('hidden');
  }

  // Game over overlay
  const goOv = document.getElementById('gameover-overlay');
  if (G.state.phase === 'game_over' && G.state.game_over) {
    goOv.classList.remove('hidden');
    const go = G.state.game_over;
    document.getElementById('go-reason').textContent = go.reason;
    const sc = document.getElementById('go-seats');
    sc.innerHTML = '';
    for (const s of go.seats) {
      const row = document.createElement('div');
      row.className = 'go-seat' + (s.won ? ' won' : '');
      row.innerHTML = `${s.seat}号 ${s.name} <span class="go-role">${s.role}（${s.faction}）${s.won ? ' 🏆' : ''}</span>`;
      sc.appendChild(row);
    }
    // Ready status for next round
    const readyCount = go.ready_count || 0;
    const total = go.total || 5;
    const me = go.seats.find(s => s.seat === G.mySeat);
    const statusEl = document.getElementById('go-ready-status');
    const btn = document.getElementById('btn-ready-again');
    statusEl.textContent = `${readyCount}/${total}人已准备`;
    if (me && me.ready) {
      btn.textContent = '已准备，等待其他玩家...';
      btn.classList.remove('btn-primary');
    } else {
      btn.textContent = '准备（下一局）';
      btn.classList.add('btn-primary');
    }
  } else {
    goOv.classList.add('hidden');
  }
}

// ===== Search Drawer =====
function openSearch() {
  document.getElementById('search-drawer').classList.remove('hidden');
  document.getElementById('search-input').value = '';
  renderSearch();
}
function closeSearch() {
  document.getElementById('search-drawer').classList.add('hidden');
}

function renderSearch() {
  const q = document.getElementById('search-input').value.trim().toLowerCase();
  const container = document.getElementById('search-results');
  let roles = G.roles;
  if (q) {
    roles = roles.filter(r => (r.name + ' ' + r.description + ' ' + (r.search||'')).toLowerCase().includes(q));
  }
  // group by faction
  const groups = { goose: [], duck: [], neutral: [] };
  for (const r of roles) groups[r.faction].push(r);
  const labels = { goose: '鹅阵营', duck: '鸭阵营', neutral: '中立阵营' };
  container.innerHTML = '';
  for (const f of ['goose','duck','neutral']) {
    if (!groups[f].length) continue;
    const lbl = document.createElement('div');
    lbl.className = 'faction-group-label';
    lbl.textContent = `${labels[f]}（${groups[f].length}）`;
    container.appendChild(lbl);
    for (const r of groups[f]) {
      const e = document.createElement('div');
      e.className = 'role-entry';
      e.innerHTML = `<span class="re-name">${r.name}</span><span class="re-faction role-faction ${r.faction}">${r.faction_name}</span><div class="re-desc">${r.description}</div>`;
      container.appendChild(e);
    }
  }
}

// ===== Mark Dialog =====
function openMarkDialog(seat) {
  const name = (G.state.seats.find(s => s.seat === seat) || {}).name || '';
  document.getElementById('mark-target-name').textContent = `${seat}号 ${name}`;
  document.getElementById('mark-dialog').classList.remove('hidden');
  document.getElementById('mark-search').value = '';
  document._markSeat = seat;
  renderMarkList();
}

function renderMarkList() {
  const q = document.getElementById('mark-search').value.trim().toLowerCase();
  const list = document.getElementById('mark-list');
  let roles = G.roles;
  if (q) roles = roles.filter(r => (r.name + ' ' + r.description).toLowerCase().includes(q));
  const currentMark = G.marks[String(document._markSeat)];
  list.innerHTML = '';
  for (const r of roles) {
    const item = document.createElement('div');
    item.className = 'mark-item' + (currentMark === r.id ? ' selected' : '');
    item.textContent = r.name + ' (' + r.faction_name + ')';
    item.onclick = () => {
      G.marks[String(document._markSeat)] = r.id;
      saveMarks();
      document.getElementById('mark-dialog').classList.add('hidden');
      renderSeats();
    };
    list.appendChild(item);
  }
}

function clearMark() {
  delete G.marks[String(document._markSeat)];
  saveMarks();
  document.getElementById('mark-dialog').classList.add('hidden');
  renderSeats();
}

// ===== Guess Dialog =====
let _guessMode = null;

function openGuessDialog(mode) {
  _guessMode = mode;
  const dialog = document.getElementById('guess-dialog');
  dialog.classList.remove('hidden');
  document.getElementById('guess-result').textContent = '';
  document.getElementById('guess-role-search').value = '';
  const title = document.getElementById('guess-title');
  const hint = document.getElementById('guess-hint');
  const targetSelect = document.getElementById('guess-target');
  // 角色选择 UI：raven 只猜目标不猜角色（隐藏）；assassin/magpie 需要选角色（显示）。
  // 显式用 '' 清除可能残留的 display:none，防止跨局/跨角色复用对话框时元素被永久隐藏。
  const roleLabel  = document.getElementById('guess-role-search').parentElement.querySelector('label:nth-of-type(2)');
  const roleSearch = document.getElementById('guess-role-search');
  const roleList   = document.getElementById('guess-role-list');
  const showRolePick = (mode !== 'raven');
  roleLabel.style.display  = showRolePick ? '' : 'none';
  roleSearch.style.display = showRolePick ? '' : 'none';
  roleList.style.display   = showRolePick ? '' : 'none';
  // build target options
  targetSelect.innerHTML = '';
  const seats = G.state.seats || [];
  for (const s of seats) {
    if (!s.name || s.seat === G.mySeat) continue;
    if (mode === 'raven' || mode === 'assassin') {
      // assassin/raven: living players
      if (s.status !== 'alive') continue;
    }
    // magpie: dead or alive, non-self
    targetSelect.innerHTML += `<option value="${s.seat}">${s.seat}号 ${s.name}</option>`;
  }
  if (mode === 'assassin') {
    title.textContent = '刺客·猜身份';
    hint.textContent = '猜对：目标被刺杀。猜错：你被刺杀。不能猜大白鹅。';
  } else if (mode === 'raven') {
    title.textContent = '渡鸦·猜目标';
    const target = G.state.day_speech.raven_target || '';
    hint.textContent = `猜谁是「${target}」。猜对：目标被刺杀。猜错：无效果。`;
  } else {
    title.textContent = '喜鹊·猜身份';
    hint.textContent = '猜对可继续猜。累计猜对2名不同玩家获胜。';
  }
  if (mode !== 'raven') {
    renderGuessRoleList();
  }
  // store selected role
  _guessSelectedRole = null;
}

let _guessSelectedRole = null;

function renderGuessRoleList() {
  const q = document.getElementById('guess-role-search').value.trim().toLowerCase();
  const list = document.getElementById('guess-role-list');
  let roles = G.roles;
  if (q) roles = roles.filter(r => (r.name + ' ' + r.description).toLowerCase().includes(q));
  // assassin can't guess great_goose
  if (_guessMode === 'assassin') roles = roles.filter(r => r.id !== 'great_goose');
  list.innerHTML = '';
  for (const r of roles) {
    const item = document.createElement('div');
    item.className = 'mark-item';
    item.textContent = r.name + ' (' + r.faction_name + ')';
    item.onclick = () => {
      list.querySelectorAll('.mark-item').forEach(i => i.classList.remove('selected'));
      item.classList.add('selected');
      _guessSelectedRole = r.id;
    };
    list.appendChild(item);
  }
}

function submitGuess() {
  const target = parseInt(document.getElementById('guess-target').value);
  if (!target) { document.getElementById('guess-result').textContent = '请选择目标'; return; }
  if (_guessMode === 'raven') {
    // raven: role is the assigned target role, sent by server already
    send({ type: 'day_guess', target, role: '' });
  } else {
    if (!_guessSelectedRole) { document.getElementById('guess-result').textContent = '请选择猜测的角色'; return; }
    send({ type: 'day_guess', target, role: _guessSelectedRole });
  }
  document.getElementById('guess-result').textContent = '猜测已提交，等待结果...';
}

function handleGuessResult(data) {
  // data: { ok, msg, correct, target_seat, guessed_role, game_over }
  const dialog = document.getElementById('guess-dialog');
  // Show result message briefly
  const gr = document.getElementById('guess-result');
  if (gr) gr.textContent = data.msg || '';
  // Magpie auto-mark on correct guess
  if (data.correct && data.target_seat && data.guessed_role && _guessMode === 'magpie') {
    G.marks[String(data.target_seat)] = data.guessed_role;
    saveMarks();
    renderSeats();
  }
  // Auto-close dialog after short delay (unless game over)
  if (!data.game_over) {
    setTimeout(() => {
      dialog.classList.add('hidden');
    }, 1200);
  }
}
