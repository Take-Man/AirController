// 起動タブ（TASKS-20261027-package 第5節）: 子プロセスの run の開始・停止と、状態（1 秒ごと）
"use strict";
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const POWER_JA = { on: "オン", off: "オフ（省電力）", probe: "オフ（手を確認中）" };
const CAMERA_JA = { running: "動作中", stopped: "停止中", starting: "起動中", error: "エラー" };
const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opt = { headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ error: `応答を読めません（${r.status}）` }));
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}

// 状態は 1 行に横に並べる（モード・遅延などはオーバーレイで見えるので出さない。作者の FB）
function item(k, v) {
  const s = document.createElement("span");
  const b = document.createElement("span"); b.className = "muted"; b.textContent = `${k} `;
  s.append(b, v);
  return s;
}

let starting = false, inApp = false;
// アプリでは、設定・チュートリアルを別の大きなウィンドウで開く（ブラウザでは、そのページへ進む）
for (const a of document.querySelectorAll("a[data-window]")) {
  a.addEventListener("click", async (e) => {
    if (!inApp) return;
    e.preventDefault();
    try { await api("/api/app/focus", { tab: a.dataset.window }); } catch (err) { location.href = a.href; }
  });
}

function render(s) {
  inApp = !!s.app;
  const live = s.live;
  const running = s.app ? s.running : s.others.length > 0;
  const chip = $("state-chip");
  chip.textContent = running ? (live ? (live.active ? "動作中・オン" : "動作中・オフ") : "起動中…") : "停止中";
  chip.classList.toggle("on", running && !!live && !!live.active);
  $("btn-start").disabled = !s.app || running || starting;
  $("btn-stop").disabled = !s.app || !(s.running || s.others.length);
  const rows = [];
  if (live) {
    rows.push(item("操作", live.active ? "オン" : POWER_JA[live.power] || live.power));
    rows.push(item("カメラ", CAMERA_JA[live.camera] || live.camera));
    if (live.settings_mode) rows.push(item("設定モード", "操作を止めています"));
  }
  if (s.uptime_s) rows.push(item("動いている時間", `${Math.floor(s.uptime_s / 60)} 分 ${s.uptime_s % 60} 秒`));
  $("status").replaceChildren(...rows);
  let msg = "";
  if (!s.app) msg = "開始・停止は、アプリの中（python -m aircontroller app）で使えます。";
  else if (s.others.length) msg = `ターミナルなどから起動した Controller が動いています（PID ${s.others.join(", ")}）。「停止」で止められます。`;
  else if (!s.running && s.code != null && s.code !== 0) msg = `Controller が終了しました（終了コード ${s.code}）。最後の出力を下に出します。`;
  $("msg").textContent = msg;
  const showTail = s.app && !s.running && s.code != null && s.code !== 0 && s.tail && s.tail.length;
  $("tail").classList.toggle("hidden", !showTail);
  if (showTail) $("tail").textContent = s.tail.join("\n") + (s.log ? `\n（全体: ${s.log}）` : "");
}

async function poll() {
  try { render(await api("/api/app/status")); } catch (e) { $("msg").textContent = `状態を読めません: ${e.message}`; }
}

$("btn-start").onclick = async () => {
  starting = true;
  $("btn-start").disabled = true;
  try {
    const r = await api("/api/app/start", {});
    if (!r.ok) $("msg").textContent = r.error;
  } catch (e) { $("msg").textContent = e.message; }
  starting = false;
  poll();
};
$("btn-stop").onclick = async () => {
  $("btn-stop").disabled = true;
  try { await api("/api/app/stop", { others: true }); } catch (e) { $("msg").textContent = e.message; }
  poll();
};
poll();
setInterval(poll, 1000);
