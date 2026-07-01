// static/app.js
let sessionId = null;
let pc = null;
let pollTimer = null;

async function startGame() {
  const playerName = document.getElementById("name").value || "Player";

  // 1) create a session via the REST API (also warms cache + DB)
  const res = await fetch("/api/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ player_name: playerName }),
  });
  const state = await res.json();
  sessionId = state.session_id;
  renderState(state);
  document.getElementById("game").hidden = false;

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
    if (state.status === "ENDED") stopPolling();
  }
}

function renderState(s) {
  document.getElementById("status").textContent = s.status;
  document.getElementById("round").textContent = s.current_round;
  document.getElementById("score").textContent = s.score;
  document.getElementById("len").textContent = s.sequence_length ?? "—";
}

async function endGame() {
  if (!sessionId) return;
  await fetch(`/api/sessions/${sessionId}/end`, { method: "POST" });
  stopPolling();
  if (pc) pc.close();
  await refreshState();
  await refreshLeaderboard();
}

function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

async function refreshLeaderboard() {
  const res = await fetch("/api/leaderboard");
  const rows = await res.json();
  const ol = document.getElementById("leaderboard");
  ol.innerHTML = "";
  rows.forEach((r) => {
    const li = document.createElement("li");
    li.textContent = `${r.player_name} — ${r.score}`;
    ol.appendChild(li);
  });
}

document.getElementById("start").addEventListener("click", startGame);
document.getElementById("end").addEventListener("click", endGame);
