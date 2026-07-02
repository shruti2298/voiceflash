// static/app.js
let sessionId = null;
let pc = null;
let pollTimer = null;
let lastScore = 0;
let lastRound = 1;

const MEDALS = ["🥇", "🥈", "🥉"];

async function startGame() {
  const playerName = document.getElementById("name").value || "Player";

  document.getElementById("setup").hidden = true;
  document.getElementById("connecting").hidden = false;

  // 1) create a session via the REST API (also warms cache + DB)
  const res = await fetch("/api/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ player_name: playerName }),
  });
  const state = await res.json();
  sessionId = state.session_id;
  lastScore = state.score;
  lastRound = state.current_round;

  // 2) open mic + WebRTC to the bot
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

  const rtcRes = await fetch("/rtc/offer", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sdp: offer.sdp, type: offer.type, player_name: playerName }),
  });
  const answer = await rtcRes.json();
  await pc.setRemoteDescription(answer);

  document.getElementById("connecting").hidden = true;
  document.getElementById("game").hidden = false;
  renderState(state);

  // 3) poll game state so the UI reflects rounds/score as they change
  pollTimer = setInterval(refreshState, 1500);
  refreshLeaderboard();
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

  document.getElementById("mic-indicator").textContent = s.status === "ACTIVE" ? "🎤" : "🔇";
  document.getElementById("mic-label").textContent =
    s.status === "ACTIVE" ? "Listening…" : "Session ended";

  lastRound = s.current_round;
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
  await refreshLeaderboard();
}

function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

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

document.getElementById("start").addEventListener("click", startGame);
document.getElementById("end").addEventListener("click", endGame);
refreshLeaderboard();
