// static/app.js
let sessionId = null;
let pc = null;
let pollTimer = null;
let lastScore = 0;
let lastRound = 1;

const MEDALS = ["🥇", "🥈", "🥉"];
// sessionStorage (not localStorage) deliberately: it's per-tab, so refreshing
// THIS tab can resume THIS tab's game, without two tabs open at once (e.g.
// for testing two players on one device) fighting over the same saved id.
const SESSION_STORAGE_KEY = "voiceflash_session_id";

function saveSession(id) {
  sessionStorage.setItem(SESSION_STORAGE_KEY, id);
}

function clearSavedSession() {
  sessionStorage.removeItem(SESSION_STORAGE_KEY);
}

function validatePlayerName() {
  const input = document.getElementById("name");
  const playerName = input.value.trim();
  const errorEl = document.getElementById("name-error");

  if (!playerName) {
    input.classList.remove("input-error");
    void input.offsetWidth; // restart the shake animation if triggered again
    input.classList.add("input-error");
    errorEl.hidden = false;
    input.focus();
    return null;
  }
  errorEl.hidden = true;
  input.classList.remove("input-error");
  return playerName;
}

async function connectVoice(theSessionId, playerName) {
  sessionId = theSessionId;
  saveSession(sessionId);

  // open mic + WebRTC to the bot
  pc = new RTCPeerConnection();
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  stream.getTracks().forEach((t) => pc.addTrack(t, stream));
  pc.ontrack = (e) => {
    const audio = new Audio();
    audio.srcObject = e.streams[0];
    audio.play();
  };

  const offer = await pc.createOffer({ offerToReceiveAudio: true });
  await pc.setLocalDescription(offer);

  // session_id here is what makes this reusable for both a fresh session
  // AND resuming an existing one — the backend resumes whatever session_id
  // it's given instead of always starting a new one.
  const rtcRes = await fetch("/rtc/offer", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sdp: offer.sdp, type: offer.type, player_name: playerName, session_id: sessionId }),
  });
  const answer = await rtcRes.json();
  await pc.setRemoteDescription(answer);

  document.getElementById("connecting").hidden = true;
  document.getElementById("game").hidden = false;

  // poll game state so the UI reflects rounds/score as they change
  pollTimer = setInterval(refreshState, 1500);
  refreshLeaderboard();
}

async function startGame() {
  const playerName = validatePlayerName();
  if (!playerName) return; // name is mandatory — don't start without one

  document.getElementById("resume").hidden = true;
  document.getElementById("setup").hidden = true;
  document.getElementById("connecting").hidden = false;

  // create a session via the REST API (also warms cache + DB)
  const res = await fetch("/api/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ player_name: playerName }),
  });
  const state = await res.json();
  lastScore = state.score;
  lastRound = state.current_round;
  renderState(state);

  await connectVoice(state.session_id, playerName);
}

async function resumeGame(savedId, playerName, state) {
  document.getElementById("resume").hidden = true;
  document.getElementById("connecting").hidden = false;
  lastScore = state.score;
  lastRound = state.current_round;
  renderState(state); // show the real round/score immediately, not "0"/"—" until the first poll
  await connectVoice(savedId, playerName);
}

async function abandonSavedSession(savedId) {
  // Properly close out the old session server-side (marks it ENDED)
  // instead of leaving it as an orphaned ACTIVE row forever.
  try {
    await fetch(`/api/sessions/${savedId}/end`, { method: "POST" });
  } catch (e) { /* best-effort cleanup — don't block starting fresh over this */ }
  clearSavedSession();
  document.getElementById("resume").hidden = true;
  document.getElementById("setup").hidden = false;
  document.getElementById("name").focus();
}

async function checkForResumableSession() {
  const savedId = sessionStorage.getItem(SESSION_STORAGE_KEY);
  if (!savedId) {
    document.getElementById("setup").hidden = false;
    document.getElementById("name").focus();
    return;
  }

  try {
    const res = await fetch(`/api/sessions/${savedId}`);
    if (!res.ok) throw new Error("session not found");
    const state = await res.json();
    if (state.status !== "ACTIVE") throw new Error("session already ended");

    document.getElementById("resume-summary").textContent =
      `${state.player_name} — Round ${state.current_round}, Score ${state.score}`;
    document.getElementById("resume-yes").onclick = () => resumeGame(savedId, state.player_name, state);
    document.getElementById("resume-no").onclick = () => abandonSavedSession(savedId);
    document.getElementById("resume").hidden = false;
  } catch (e) {
    // Stale/invalid/ended — nothing to resume, fall back to the normal start screen.
    clearSavedSession();
    document.getElementById("setup").hidden = false;
    document.getElementById("name").focus();
  }
}

async function refreshState() {
  if (!sessionId) return;
  const res = await fetch(`/api/sessions/${sessionId}`);
  if (res.ok) {
    const state = await res.json();
    renderState(state);
    if (state.status === "ENDED") {
      stopPolling();
      showGameOver(state);
      refreshLeaderboard();
      clearSavedSession(); // nothing left to resume once the game is over
    }
  }
}

function renderState(s) {
  const badge = document.getElementById("status-badge");
  badge.textContent = s.status === "ACTIVE" ? "● ACTIVE" : "● ENDED";
  badge.className = "badge " + (s.status === "ACTIVE" ? "badge-active" : "badge-ended");

  document.getElementById("round").textContent = s.current_round;

  const scoreEl = document.getElementById("score");
  if (s.score !== lastScore) {
    scoreEl.textContent = s.score;
    scoreEl.classList.remove("bump");
    void scoreEl.offsetWidth; // restart animation
    scoreEl.classList.add("bump");
    if (s.score > lastScore) showFeedback("✨ Correct!", "correct");
    lastScore = s.score;
  } else {
    scoreEl.textContent = s.score;
  }

  renderCards(s.sequence_length);
  renderLastRound(s);

  document.getElementById("mic-indicator").textContent = s.status === "ACTIVE" ? "🎤" : "🔇";
  document.getElementById("mic-label").textContent =
    s.status === "ACTIVE" ? "Listening…" : "Session ended";

  lastRound = s.current_round;
}

function renderLastRound(s) {
  const panel = document.getElementById("last-round");
  if (!s.last_expected) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  panel.className = "last-round " + (s.last_correct ? "correct" : "wrong");
  document.getElementById("last-expected").textContent = s.last_expected.join(", ");
  document.getElementById("last-heard").textContent = (s.last_heard || []).join(", ");
}

function renderCards(count) {
  const cardsEl = document.getElementById("cards");
  cardsEl.innerHTML = "";
  if (!count) return;
  for (let i = 0; i < count; i++) {
    const c = document.createElement("div");
    c.className = "card";
    cardsEl.appendChild(c);
  }
}

function showFeedback(text, kind) {
  const el = document.getElementById("feedback");
  el.textContent = text;
  el.className = `feedback ${kind}`;
  void el.offsetWidth;
  el.classList.add("show");
  burstConfetti();
  setTimeout(() => { el.textContent = ""; el.className = "feedback"; }, 2200);
}

function showGameOver(s) {
  showFeedback("💥 Game over!", "wrong");
  document.getElementById("final-score").textContent = s.score;
  document.getElementById("game-over").hidden = false;
  document.getElementById("mic-row")?.setAttribute("hidden", "true");
}

async function endGame() {
  if (!sessionId) return;
  const res = await fetch(`/api/sessions/${sessionId}/end`, { method: "POST" });
  const state = await res.json();
  stopPolling();
  if (pc) pc.close();
  renderState(state);
  showGameOver(state);
  clearSavedSession();
  await refreshLeaderboard();
}

function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

function playAgain() {
  stopPolling();
  if (pc) {
    try { pc.close(); } catch (e) { /* already closed */ }
    pc = null;
  }
  sessionId = null;
  clearSavedSession(); // defensive — normally already cleared by endGame()/refreshState()
  lastScore = 0;
  lastRound = 1;

  // Reset the game panel back to its pre-game appearance so the next
  // session starts clean instead of showing stale round/score/feedback.
  document.getElementById("game").hidden = true;
  document.getElementById("game-over").hidden = true;
  document.getElementById("mic-row").hidden = false;
  document.getElementById("end").hidden = false;
  document.getElementById("last-round").hidden = true;
  document.getElementById("feedback").textContent = "";
  document.getElementById("feedback").className = "feedback";
  document.getElementById("cards").innerHTML = "";
  document.getElementById("score").textContent = "0";
  document.getElementById("round").textContent = "—";

  document.getElementById("setup").hidden = false;
  document.getElementById("name").focus();
}

async function refreshLeaderboard() {
  const res = await fetch("/api/leaderboard");
  const rows = await res.json();
  const ol = document.getElementById("leaderboard");
  ol.innerHTML = "";
  if (rows.length === 0) {
    ol.innerHTML = '<li class="leaderboard-empty">No games finished yet — be the first! 🚀</li>';
    return;
  }
  rows.forEach((r, i) => {
    const li = document.createElement("li");
    const rank = MEDALS[i] || `#${i + 1}`;
    li.innerHTML = `<span><span class="rank">${rank}</span>${escapeHtml(r.player_name)}</span><span class="lb-score">${r.score}</span>`;
    ol.appendChild(li);
  });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function burstConfetti() {
  const container = document.getElementById("confetti");
  const colors = ["#7c5cff", "#ff5c93", "#ffb020", "#2ecc71"];
  for (let i = 0; i < 18; i++) {
    const piece = document.createElement("span");
    const size = 6 + Math.random() * 6;
    piece.style.position = "fixed";
    piece.style.top = "-10px";
    piece.style.left = Math.random() * 100 + "vw";
    piece.style.width = size + "px";
    piece.style.height = size + "px";
    piece.style.background = colors[i % colors.length];
    piece.style.borderRadius = Math.random() > 0.5 ? "50%" : "2px";
    piece.style.opacity = "0.9";
    piece.style.transform = `rotate(${Math.random() * 360}deg)`;
    piece.style.transition = "transform 1.1s ease-in, top 1.1s ease-in, opacity 1.1s ease-in";
    piece.style.pointerEvents = "none";
    container.appendChild(piece);
    requestAnimationFrame(() => {
      piece.style.top = 90 + Math.random() * 10 + "vh";
      piece.style.transform = `rotate(${Math.random() * 720}deg)`;
      piece.style.opacity = "0";
    });
    setTimeout(() => piece.remove(), 1300);
  }
}

const nameInput = document.getElementById("name");
document.getElementById("start").addEventListener("click", startGame);
document.getElementById("end").addEventListener("click", endGame);
document.getElementById("play-again").addEventListener("click", playAgain);
nameInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") startGame();
});
nameInput.addEventListener("input", () => {
  // Clear the error state as soon as they start fixing it, instead of
  // leaving a stale red border/message up while they type.
  if (nameInput.value.trim()) {
    nameInput.classList.remove("input-error");
    document.getElementById("name-error").hidden = true;
  }
});
checkForResumableSession(); // decides whether to show the resume banner or the setup screen
refreshLeaderboard();

// ---- How to Play: collapsible panel + interactive step tabs ----
function setupHowToPlay() {
  const panel = document.getElementById("howto");
  const toggleBtn = document.getElementById("howto-toggle");

  toggleBtn.addEventListener("click", () => {
    const collapsed = panel.classList.toggle("collapsed");
    toggleBtn.setAttribute("aria-expanded", String(!collapsed));
    if (!collapsed) localStorage.removeItem("howtoCollapsed");
    else localStorage.setItem("howtoCollapsed", "1");
  });

  // Remember dismissal across visits, but always show it the first time.
  if (localStorage.getItem("howtoCollapsed") === "1") {
    panel.classList.add("collapsed");
    toggleBtn.setAttribute("aria-expanded", "false");
  }

  const chips = document.querySelectorAll(".step-chip");
  const details = document.querySelectorAll("[data-step-detail]");
  chips.forEach((chip) => {
    chip.addEventListener("click", () => {
      const step = chip.dataset.step;
      chips.forEach((c) => c.classList.toggle("active", c === chip));
      details.forEach((d) => d.classList.toggle("active", d.dataset.stepDetail === step));
    });
  });
}

setupHowToPlay();
