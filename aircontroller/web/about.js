// AirController について（TASKS-20261027-package 第8節）: 開き方・データの扱い・アンインストール。データの場所と残るファイルはサーバーから
"use strict";
const TOKEN = new URLSearchParams(location.search).get("t") || "";
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

(async () => {
  const info = await api("/api/app/info");
  $("version").textContent = info.version ? `版 ${info.version}` : "ソースから動かしています";
  $("home-dir").textContent = info.home;
  $("leftovers").replaceChildren(...info.leftovers.map((p) => { const li = document.createElement("li"); li.textContent = p; return li; }));
  if (info.app) {
    $("btn-reveal").classList.remove("hidden");
    $("btn-reveal").onclick = () => api("/api/app/reveal", {}).catch(() => {});
  }
})().catch((e) => { $("version").textContent = `読み込めませんでした: ${e.message}`; });
