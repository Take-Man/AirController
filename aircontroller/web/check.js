// 環境のチェック（TASKS-20261027-package 第4節）。「テスト開始」で項目を順番に動かし、全項目の結果を出す。
"use strict";
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const STATUS_JA = { ok: "合格", warn: "要確認", fail: "失敗", skip: "未確認", running: "確認中" };
let ITEMS = [], MARK = null, busy = false;

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false && c !== "") e.append(c instanceof Node ? c : String(c));
  return e;
}

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

const $ = (id) => document.getElementById(id);
const rows = {};

// 結果の 1 行: 判定・項目・ひとこと。「詳細」を開くと、調べた内容と直し方（印のキーは「設定する」も）
function row(res) {
  const li = el("li", { "data-id": res.id });
  const body = [res.detail && res.detail.length ? el("ul", {}, res.detail.map((d) => el("li", {}, d))) : null,
    res.fix ? el("div", { class: "fix" }, res.fix) : null, res.id === "mark_key" ? markKeyTools() : null].filter(Boolean);
  li.append(el("div", { class: "check-head" }, el("span", { class: `badge ${res.status}` }, STATUS_JA[res.status] || res.status),
    el("strong", {}, res.title), el("span", { class: "muted" }, res.summary)),
    body.length ? el("details", { class: "detail" }, el("summary", {}, "詳細"), ...body) : null);
  li._result = res;
  rows[res.id] = li;
  return li;
}

function totals() {
  const n = { ok: 0, warn: 0, fail: 0, skip: 0 };
  for (const li of Object.values(rows)) n[li._result.status] = (n[li._result.status] || 0) + 1;
  $("totals").replaceChildren(...["ok", "warn", "fail", "skip"].filter((k) => n[k]).map((k) =>
    el("span", {}, el("span", { class: `badge ${k}` }, STATUS_JA[k]), ` ${n[k]}`)));
  return n;
}

async function runItem(item, i) {
  $("progress-fill").style.width = `${(i / ITEMS.length) * 100}%`;
  let input = null;
  const now = [el("strong", {}, `${i + 1} / ${ITEMS.length}  ${item.title}`), el("span", { class: "muted" }, item.desc || "")];
  if (item.prompt) {
    input = el("input", { type: "text", placeholder: "ここにカーソルを置いたまま、キーを押す" });
    now.push(el("div", { class: "prompt" }, item.prompt.replace("{key}", MARK ? MARK.label : "印のキー"), input));
  }
  $("now").replaceChildren(...now);
  if (input) input.focus();
  let res;
  try {
    res = await api("/api/check/item", { id: item.id });
  } catch (e) {
    res = { id: item.id, title: item.title, status: "fail", summary: `確かめられませんでした: ${e.message}`, detail: [], fix: "" };
  }
  if (item.id === "mark_key" && res.status === "ok" && input) {
    // 握りつぶし: 押したキーの文字が入力欄に入らなければ合格
    if (input.value !== "") {
      res.status = "warn";
      res.summary += ` ただし、文字（${input.value}）が入力されました。`;
      res.fix = "アクセシビリティの権限を確かめてください（キーの握りつぶしには、入力監視に加えてアクセシビリティが要ります）。";
    } else {
      res.summary += " 文字は入力されませんでした。";
    }
  }
  $("list").append(row(res));
  $("list-summary").textContent = `項目ごとの結果（${$("list").children.length} / ${ITEMS.length}）`;
  totals();
}

// 画面の中の確認のダイアログ（アプリのウィンドウでは confirm() が使えない）
function confirmBox(title, body, okLabel) {
  const d = $("confirm");
  $("confirm-title").textContent = title;
  $("confirm-body").replaceChildren(el("p", {}, body));
  $("confirm-ok").textContent = okLabel;
  return new Promise((res) => {
    d.addEventListener("close", () => res(d.returnValue === "ok"), { once: true });
    d.returnValue = "";
    d.showModal();
  });
}

async function start() {
  if (busy) return;
  $("menu-msg").textContent = "";
  let r = await api("/api/check/start", {});
  if (r.need_stop) {
    const ok = await confirmBox("run を止めて確かめます", "カメラとキーの監視が重なるので、動いている run（PID " + r.run.join(", ")
      + "）を止めてから確かめます（確かめた後は、ホームの「開始」で動かし直してください）", "止めて確かめる");
    if (!ok) return;
    $("menu-msg").textContent = "run を止めています…";
    r = await api("/api/check/start", { stop_run: true });
    $("menu-msg").textContent = "";
  }
  busy = true;
  $("menu").classList.add("hidden");
  $("run-panel").classList.remove("hidden");
  $("btn-again").classList.add("hidden");
  $("now").classList.remove("hidden");
  $("list-wrap").open = false;
  $("run-title").textContent = "チェック中…";
  for (const k of Object.keys(rows)) delete rows[k];
  $("list").replaceChildren();
  $("totals").replaceChildren();
  for (const [i, item] of ITEMS.entries()) await runItem(item, i);
  $("progress-fill").style.width = "100%";
  $("now").classList.add("hidden");
  const n = totals();
  $("run-title").textContent = n.fail ? "結果: 失敗があります" : n.warn ? "結果: 要確認があります" : "結果: すべて合格";
  $("btn-again").classList.remove("hidden");
  $("list-summary").textContent = `項目ごとの結果（${ITEMS.length} 項目）`;
  busy = false;
}

// 印のキーの設定: 「設定する」→ キーの入力待ち → 確認 → 反映
function markKeyTools() {
  const box = el("div", { class: "prompt" });
  const msg = el("div", { class: "muted small" }, `今の印のキー: ${MARK ? MARK.label : "（なし）"}。¥ のキーがない・検出できないときは、代わりのキーを設定できます。`);
  const btn = el("button", { onclick: () => capture(box, msg, btn) }, "設定する");
  box.append(msg, el("div", { class: "row wrap" }, btn));
  return box;
}

async function capture(box, msg, btn) {
  btn.disabled = true;
  msg.textContent = "印に使うキーを押してください（10 秒待ちます。押したキーの文字は入力されません）。";
  let r;
  try {
    r = await api("/api/check/capture_key", {});
  } catch (e) {
    msg.textContent = `キーを受け取れませんでした: ${e.message}`;
    btn.disabled = false;
    return;
  }
  btn.disabled = false;
  if (r.error) { msg.textContent = r.error; return; }
  if (r.timeout) { msg.textContent = "キーが押されませんでした。もう一度「設定する」を押してください。"; return; }
  if (r.level === "refuse") { msg.textContent = `${r.label}: ${r.reason}`; return; }
  msg.replaceChildren(el("strong", {}, `${r.label}（キーコード ${r.keycode}）`), ` を印のキーにしますか？ ${r.reason}`);
  const yes = el("button", { class: "primary", onclick: async () => {
    try {
      const out = await api("/api/check/set_mark_key", { keycode: r.keycode });
      MARK = out.mark_key;
      msg.textContent = `印のキーを ${MARK.label} にしました（calibration.toml に保存。run を起動し直すと使われます）。「もう一度」で確かめられます。`;
    } catch (e) { msg.textContent = `保存できませんでした: ${e.message}`; }
    actions.remove();
  } }, "このキーを使う");
  const no = el("button", { class: "ghost", onclick: () => { msg.textContent = "やめました。"; actions.remove(); } }, "やめる");
  const actions = el("div", { class: "row wrap" }, yes, no);
  box.append(actions);
}

async function init() {
  const r = await api("/api/check/items");
  ITEMS = r.items;
  MARK = r.mark_key;
  if (r.run.length) $("menu-msg").textContent = `run が動いています（PID ${r.run.join(", ")}）。テスト開始のときに止めます。`;
  $("btn-start").onclick = () => start().catch((e) => { busy = false; $("menu-msg").textContent = e.message; });
  $("btn-again").onclick = () => start().catch((e) => { busy = false; $("run-title").textContent = `確かめられませんでした: ${e.message}`; });  // 再チェック
}
init().catch((e) => { $("menu-msg").textContent = `読み込めませんでした: ${e.message}`; });
