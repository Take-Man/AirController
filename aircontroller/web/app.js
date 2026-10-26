"use strict";
// AirController 設定画面（SPEC 13.46）。出すのは区分 A と B だけ（C・D は画面に出さず、サーバーも受け付けない）。
// B（測定で決まる値）と発火の位置は、分布図や run の操作中の UI の上で、線や円をドラッグして変更する。

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const WARN = 0.15; // 変化量の警告（保存済みから 15% 以上）
const UNITS = [["range", "U1 操作範囲"], ["pinch", "U2 左ピンチ"], ["right", "U3 右クリック"], ["peace", "U4 ピース"],
  ["thumbsup", "U5 いいね"], ["zoom", "U6 ズーム"]];
const GJA = { cursor: "カーソル", pinch: "ピンチ", right: "右ピンチ", peace: "ピース", thumbsup: "いいね", zoom: "ズーム", fist: "グー" };
const MJA = { click: "クリック", right_click: "右クリック", drag: "ドラッグ", drag_suspect: "意図しないドラッグの候補",
  canceled: "取り消されたモード", nav: "戻る・進む", thumbsup: "いいね", zoom: "ズーム（段）", scroll_px: "スクロール量（px）" };
const HAND_SEG = [["left", "左手"], ["both", "両手"], ["right", "右手"]];

// 画面の並び。文字列は項目、{w: ...} は図（ウィジェット）
// { pair: [節, 節] } は節を横に並べる。項目の { row: [...] } は横並び、{ col: [...] } は縦並び
const LAYOUT = [
  { pair: [{ title: "使う手とモード", items: ["gesture.hand", "mode.posture_hold_ms"], flex: 1 },
    { title: "オン・オフと省電力", items: ["power.wake_enabled", "power.wake_hold_s", "power.auto_off_after_s", "power.idle_stop_s"], flex: 3 }] },
  { title: "カーソルと操作範囲", units: ["range"], items: [{ w: "roi" }, { g: "cursorSens" }, { g: "roiAdapt" }] },
  { title: "クリック・ピンチ・右クリック", units: ["pinch", "right"], items: [
    { row: [{ w: "pinch", stack: true }, "click.drag_ms"], cls: "with-dial" },
    { row: [{ w: "right", stack: true }, "click.right_hold_ms"], cls: "with-dial" }] },
  { title: "ピース（スクロール、戻る・進む）", toggle: "peace.peace_enabled", units: ["peace"], items: [{ w: "peaceRow" },
    { w: "thumbfold" }, { w: "scrollCurve", stack: false, belowLayout: { row: [{ col: ["peace.scroll_invert", "peace.nav_swap"] },
      "peace.nav_dwell_ms"], cls: "scroll-extra" } }] },
  { title: "いいね（デスクトップ移動、Mission Control）", toggle: "thumbsup.thumbsup_enabled", units: ["thumbsup"],
    items: [{ w: "radar" }, { w: "far" }, "thumbsup.thumbsup_dwell_ms"] },
  { title: "ズーム", toggle: "zoom.zoom_enabled", units: ["zoom"], items: [{ w: "zoom", belowDial: "zoom.zoom_fire_hold_ms" }] },
  { title: "グー（何もしない）", toggle: "fist.fist_enabled", items: [] },
];
const UNIT_SHORT = { range: "U1 測定", pinch: "U2 測定", right: "U3 測定", peace: "U4 測定", thumbsup: "U5 測定", zoom: "U6 測定" };
const isTime = (p) => p && (p.unit === "ms" || p.unit === "秒"); // 時間の項目は円のダイヤルにする

let S = null; // サーバーの状態
const edits = {}; // 変更中の値 {id: value}
const dists = {}; // 測定の分布 {unit: {...}}
const byId = {};
const widgets = []; // {keys, update}
let toggleOf = {}; // 項目 → それが属する機能のトグル

// ---------------------------------------------------------------- 通信と部品
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

const NS = "http://www.w3.org/2000/svg";
function svgEl(parent, tag, at = {}, text) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(at)) if (v !== undefined && v !== null && v !== "") e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  if (parent) parent.append(e);
  return e;
}
function newSvg(w, h, sq = false) {
  const s = document.createElementNS(NS, "svg");
  s.setAttribute("viewBox", `0 0 ${w} ${h}`);
  s.classList.add("fig");
  if (sq) s.classList.add("sq"); // 円や棒の図は、広げすぎない
  return s;
}
// 図が横に伸びても文字が大きくならないように、伸びの逆数を --k に入れる（CSS で文字の大きさに掛ける）
const textScaler = typeof ResizeObserver !== "undefined" ? new ResizeObserver((entries) => {
  for (const e of entries) if (e.contentRect.width > 0) e.target.style.setProperty("--k", String(e.target._vbw / e.contentRect.width));
}) : null;
function keepTextSize(svg, viewBoxWidth) {
  svg._vbw = viewBoxWidth;
  if (textScaler) textScaler.observe(svg);
}
function svgPoint(svg, ev) {
  const p = svg.createSVGPoint();
  p.x = ev.clientX; p.y = ev.clientY;
  return p.matrixTransform(svg.getScreenCTM().inverse());
}
// ドラッグ: pick(pt) がつかんだもの（無ければ null）を返し、move(obj, pt) で値を変える
function draggable(svg, pick, move) {
  let held = null;
  svg.addEventListener("pointerdown", (ev) => {
    held = pick(svgPoint(svg, ev));
    if (held) { svg.setPointerCapture(ev.pointerId); svg.classList.add("dragging"); ev.preventDefault(); move(held, svgPoint(svg, ev)); }
  });
  svg.addEventListener("pointermove", (ev) => {
    if (held) move(held, svgPoint(svg, ev));
    else svg.classList.toggle("grab", !!pick(svgPoint(svg, ev)));
  });
  const end = () => { held = null; svg.classList.remove("dragging"); };
  svg.addEventListener("pointerup", end);
  svg.addEventListener("pointercancel", end);
}

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 4000);
}

function confirmBox(title, body, okLabel = "実行する") {
  const d = document.getElementById("confirm");
  document.getElementById("confirm-title").textContent = title;
  document.getElementById("confirm-body").replaceChildren(...(Array.isArray(body) ? body : [body]).filter(Boolean));
  document.getElementById("confirm-ok").textContent = okLabel;
  return new Promise((res) => {
    d.addEventListener("close", () => res(d.returnValue === "ok"), { once: true });
    d.showModal();
  });
}

// ---------------------------------------------------------------- 値
const num = (v) => typeof v === "number" && isFinite(v);
function decimals(p) { return p && p.step ? Math.max(0, Math.ceil(-Math.log10(p.step) - 1e-9)) : 4; }
function fmt(v, p) {
  if (typeof v === "boolean") return v ? "オン" : "オフ";
  if (p && p.id === "gesture.hand") return (HAND_SEG.find(([k]) => k === v) || [v, v])[1];
  if (num(v)) {
    if (!p || !p.step) return String(+v.toFixed(4));
    const s = v.toFixed(decimals(p));
    return +s === v ? s : String(+v.toFixed(4)); // 測定の値など、刻みに乗らない値は 4 桁まで
  }
  return v === undefined || v === null ? "—" : String(v);
}
const cur = (id) => (id in edits ? edits[id] : byId[id].saved);
const isOffZero = (id) => S.off_at_zero.includes(id);
function rel(p, a, b) {
  if (!num(a) || !num(b) || a === b) return null;
  if (a !== 0) return (b - a) / Math.abs(a);
  return p.lo !== null ? (b - a) / (p.hi - p.lo) : null;
}
const warned = (p, a, b) => { const r = rel(p, a, b); return r !== null && Math.abs(r) >= WARN - 1e-9; };
const pct = (r) => (r === null ? "" : `${r >= 0 ? "+" : ""}${Math.round(r * 100)}%`);
function snap(p, v) {
  if (!num(v) || p.lo === null) return v;
  v = Math.min(p.hi, Math.max(p.lo, v));
  const n = Math.round((v - p.lo) / p.step);
  return +(p.lo + n * p.step).toFixed(decimals(p) + 2);
}
function changedIds() { return Object.keys(edits).filter((id) => byId[id] && edits[id] !== byId[id].saved); }

function setEdit(id, v, { exact = false } = {}) {
  const p = byId[id];
  if (!p) return;
  if (num(v) && !(isOffZero(id) && v === 0)) v = exact ? Math.min(p.hi ?? v, Math.max(p.lo ?? v, v)) : snap(p, v);
  if (v === p.saved) delete edits[id]; else edits[id] = v;
  refreshAll(id);
  sendLiveConfig();
}

function refreshAll(changedId) {
  for (const w of widgets) if (!changedId || w.keys.includes(changedId)) w.update();
  for (const r of document.querySelectorAll(".item")) if (r._refresh && (!changedId || r.dataset.id === changedId)) r._refresh();
  for (const tr of document.querySelectorAll("tr.keyrow")) if (tr._refresh && (!changedId || tr.dataset.id === changedId)) tr._refresh();
  applyToggles();
  refreshTop();
}

function applyToggles() {
  for (const [t, members] of Object.entries(S.toggles)) {
    const on = !!cur(t);
    for (const m of members) document.querySelectorAll(`.item[data-id="${m}"]`).forEach((e) => e.classList.toggle("off", !on));
    document.querySelectorAll(`[data-group~="${t}"]`).forEach((e) => e.classList.toggle("off", !on));
  }
}

// ---------------------------------------------------------------- 部品: トグル、セグメント、項目の行
// トグルスイッチは「有効 / 無効」の切り替えだけに使い、横に今の状態を文字で出す。sw._set(on) で表示を合わせる
function toggleSwitch(checked, onchange, label) {
  const state = el("span", { class: "sw-state" });
  const input = el("input", { type: "checkbox", role: "switch", "aria-label": label || "",
    onchange: (e) => { state.textContent = e.target.checked ? "有効" : "無効"; onchange(e.target.checked); } });
  const box = el("span", { class: "switch-wrap" }, state, el("label", { class: "switch" }, input, el("span", { class: "knob" })));
  box._set = (on) => { input.checked = !!on; state.textContent = on ? "有効" : "無効"; box.classList.toggle("is-off", !on); };
  box._set(checked);
  return box;
}

function segmented(id, options) {
  const box = el("div", { class: "seg", role: "radiogroup" });
  for (const [v, label] of options) box.append(el("button", { type: "button", role: "radio", "data-v": String(v), onclick: () => setEdit(id, v) }, label));
  box._refresh = () => box.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.v === String(cur(id)))));
  return box;
}

function valueLine(p, withSaved = true) {
  const v = cur(p.id);
  const changed = v !== p.saved;
  return [
    withSaved ? el("span", { class: "s" }, `● 保存済み ${fmt(p.saved, p)}`) : null,
    withSaved && p.measured ? el("span", { class: "m" }, `▲ 測定 ${fmt(p.measured.value, p)}`) : null,
    changed ? el("span", { class: "e" }, `変更中 ${fmt(v, p)} ${pct(rel(p, p.saved, v))}`) : null,
    changed && warned(p, p.saved, v) ? el("span", { class: "warn" }, `⚠ ${Math.round(WARN * 100)}% 以上`) : null,
  ].filter(Boolean);
}

// 向きを選ぶ項目（真偽）: 問いかけの文と、選択肢のセグメント
const SEG_ITEMS = {
  "peace.scroll_invert": { q: "ピースを上に動かしたとき", opts: [[false, "下に移動"], [true, "上に移動"]] },
  "peace.nav_swap": { q: "ピースを右に動かしたとき", opts: [[true, "戻る"], [false, "進む"]] },
};

// opt.label: 名前の代わりに出す文字、opt.ends: スライダーの左右の端の説明 [左, 右]
function itemRow(id, opt = {}) {
  const p = byId[id];
  if (!p) return el("div");
  const box = el("div", { class: "item", "data-id": id });
  const name = el("span", { class: "name" }, opt.label || p.name);
  if (SEG_ITEMS[id]) {
    const seg = segmented(id, SEG_ITEMS[id].opts);
    const vals = el("div", { class: "vals" });
    box.append(el("div", { class: "line" }, el("span", { class: "name" }, SEG_ITEMS[id].q), el("span", { class: "spacer" }), seg), vals);
    box._refresh = () => {
      seg._refresh();
      const lab = (v) => SEG_ITEMS[id].opts.find(([k]) => k === v)[1];
      vals.replaceChildren(...(cur(id) !== p.saved ? [el("span", { class: "s" }, `● 保存済み ${lab(p.saved)}`), el("span", { class: "e" }, `変更中 ${lab(cur(id))}`)] : []));
      box.classList.toggle("changed", cur(id) !== p.saved);
    };
    return box;
  }
  const vals = el("div", { class: "vals" });
  const reset = el("button", { class: "small ghost", onclick: () => setEdit(id, p.saved) }, "保存済みに戻す");
  if (typeof p.default === "boolean") {
    const sw = toggleSwitch(!!cur(id), (on) => setEdit(id, on), p.name);
    box.append(el("div", { class: "line" }, name, el("span", { class: "spacer" }), vals, sw));
    box._refresh = () => {
      sw._set(!!cur(id));
      vals.replaceChildren(...(cur(id) !== p.saved ? [el("span", { class: "e" }, "変更あり（未保存）")] : []));
      box.classList.toggle("changed", cur(id) !== p.saved);
    };
  } else if (id === "gesture.hand") {
    const seg = segmented(id, HAND_SEG);
    box.append(el("div", { class: "line" }, name, el("span", { class: "spacer" }), seg), vals);
    box._refresh = () => { seg._refresh(); vals.replaceChildren(...valueLine(p)); box.classList.toggle("changed", cur(id) !== p.saved); };
  } else {
    const range = el("input", { type: "range", min: p.lo, max: p.hi, step: p.step, oninput: (e) => setEdit(id, +e.target.value) });
    const ms = el("span", { class: "marker saved", title: "保存済み" }, "●");
    const mm = el("span", { class: "marker measured hidden", title: "測定" }, "▲");
    const track = el("div", { class: "track" }, ms, mm, range,
      opt.ends ? el("div", { class: "ends" }, el("span", {}, `← ${opt.ends[0]}`), el("span", {}, `${opt.ends[1]} →`)) : null);
    const box2 = el("input", { type: "number", min: p.lo, max: p.hi, step: "any",
      onchange: (e) => { if (e.target.value !== "") setEdit(id, +e.target.value, { exact: true }); } });
    let sw = null;
    if (isOffZero(id)) sw = toggleSwitch(cur(id) !== 0, (on) => setEdit(id, on ? (p.saved || p.default || p.lo) : 0), `${p.name}を有効にする`);
    box.append(el("div", { class: "line" }, name, el("span", { class: "spacer" }), sw),
      el("div", { class: "ctl" }, track, box2, el("span", { class: "muted unit" }, p.unit)),
      el("div", { class: "line" }, vals, el("span", { class: "spacer" }), reset));
    box._refresh = () => {
      const v = cur(id);
      const off = isOffZero(id) && v === 0;
      range.disabled = box2.disabled = off;
      box.classList.toggle("zero", off);
      if (!off) { range.value = v; if (document.activeElement !== box2) box2.value = fmt(v, p); } else box2.value = "";
      if (sw) sw._set(!off);
      const pos = (x) => `${((x - p.lo) / (p.hi - p.lo)) * 100}%`;
      ms.classList.toggle("hidden", !(num(p.saved) && p.saved >= p.lo));
      ms.style.left = pos(p.saved);
      if (p.measured && num(p.measured.value)) { mm.classList.remove("hidden"); mm.style.left = pos(Math.min(p.hi, Math.max(p.lo, p.measured.value))); }
      else mm.classList.add("hidden");
      vals.replaceChildren(...valueLine(p), ...(off ? [el("span", { class: "muted" }, "無効")] : []));
      box.classList.toggle("changed", v !== p.saved);
      reset.disabled = v === p.saved;
    };
  }
  return box;
}

// 小見出しのグループ: 項目を横に並べる。toggle があれば小見出しの横にスイッチ
const GROUPS = {
  cursorSens: { title: "カーソルの反映度", items: [
    ["cursor.cursor_min_cutoff", "手の動きが遅いとき", ["揺れにくい・遅れる", "すぐ反映・揺れやすい"]],
    ["cursor.cursor_beta", "手の動きが速いとき", ["なめらか・遅れる", "すぐ追従・行き過ぎやすい"]]] },
  roiAdapt: { title: "操作範囲の自動補正", toggle: "cursor.roi_adapt", items: [
    ["cursor.roi_adapt_shift", "位置の追従", ["ゆっくり", "すぐ"]],
    ["cursor.roi_adapt_grow", "大きさの追従", ["ゆっくり", "すぐ"]]] },
};
function groupBox(key) {
  const g = GROUPS[key];
  const head = el("div", { class: "ghead" }, el("h3", {}, g.title));
  if (g.toggle) {
    const sw = toggleSwitch(!!cur(g.toggle), (on) => setEdit(g.toggle, on), `${g.title}を有効にする`);
    head.append(el("span", { class: "spacer" }), sw);
    widgets.push({ keys: [g.toggle], update: () => sw._set(!!cur(g.toggle)) });
  }
  return el("div", { class: "group" }, head,
    el("div", { class: "group-grid" }, g.items.map(([id, label, ends]) => itemRow(id, { label, ends }))));
}

// 時間の項目: 円のダイヤル。中央に数値の入力と（0 で無効にできる項目は）スイッチ。下が空いた 270° の円で、左下が最小、右下が最大
const DIAL = { size: 150, r: 60, a0: 225, sweep: 270 };
function dialAngle(t) { return DIAL.a0 - DIAL.sweep * t; }
function dialArc(c, r, t0, t1) { // t0 < t1 の範囲の弧（時計回りに t0 → t1）
  const a0 = dialAngle(t0), a1 = dialAngle(t1);
  const [x0, y0] = polar(c, c, r, a0), [x1, y1] = polar(c, c, r, a1);
  return `M${x0},${y0} A${r},${r} 0 ${a0 - a1 > 180 ? 1 : 0} 1 ${x1},${y1}`;
}
function dialCard(id) {
  const p = byId[id];
  const C = DIAL.size / 2, R = DIAL.r;
  const svg = newSvg(DIAL.size, DIAL.size);
  svg.classList.add("dial");
  const tOf = (v) => Math.min(1, Math.max(0, (v - p.lo) / (p.hi - p.lo)));
  svgEl(svg, "path", { class: "dial-track", d: dialArc(C, R, 0, 1) });
  const val = svgEl(svg, "path", { class: "dial-val" });
  const mS = svgEl(svg, "circle", { class: "dot-saved", r: 3.5 });
  const mM = svgEl(svg, "path", { class: "mk-measured" });
  const knob = svgEl(svg, "circle", { class: "dial-knob", r: 7 });
  const inp = el("input", { type: "number", step: "any", min: p.lo, max: p.hi, "aria-label": p.name,
    onchange: (e) => { if (e.target.value !== "") setEdit(id, +e.target.value, { exact: true }); } });
  const sw = isOffZero(id) ? toggleSwitch(cur(id) !== 0, (on) => setEdit(id, on ? (p.saved || p.default || p.lo) : 0), `${p.name}を有効にする`) : null;
  const center = el("div", { class: "dial-center" }, el("div", { class: "dial-num" }, inp, el("span", { class: "muted small" }, p.unit)), sw);
  const vals = el("div", { class: "vals center" });
  const reset = el("button", { class: "small ghost", onclick: () => setEdit(id, p.saved) }, "保存済みに戻す");
  const card = el("div", { class: "item dial-card", "data-id": id }, el("div", { class: "name" }, p.name),
    el("div", { class: "dial-box" }, svg, center), vals, reset);
  const off = () => isOffZero(id) && cur(id) === 0;
  draggable(svg, (pt) => {
    if (off()) return null;
    return Math.abs(Math.hypot(pt.x - C, pt.y - C) - R) < 16 ? {} : null;
  }, (o, pt) => {
    let a = (Math.atan2(C - pt.y, pt.x - C) * 180) / Math.PI;
    if (a < -135) a += 360; // -135..225 にそろえる（下の空き: -135〜-45）
    let t = (DIAL.a0 - a) / DIAL.sweep;
    if (t > 1) t = t > 1 + (90 / DIAL.sweep) / 2 ? 0 : 1; // 下の空きでは、近い方の端
    setEdit(id, p.lo + t * (p.hi - p.lo));
  });
  card._refresh = () => {
    const v = cur(id), isOff = off();
    card.classList.toggle("zero", isOff);
    inp.disabled = isOff;
    if (sw) sw._set(!isOff);
    if (document.activeElement !== inp) inp.value = isOff ? "" : fmt(v, p);
    const t = isOff ? 0 : tOf(v);
    val.setAttribute("d", t > 0.001 ? dialArc(C, R, 0, t) : "");
    const [kx, ky] = polar(C, C, R, dialAngle(t));
    knob.setAttribute("cx", kx); knob.setAttribute("cy", ky);
    knob.classList.toggle("hidden", isOff);
    const [sx, sy] = polar(C, C, R + 11, dialAngle(tOf(p.saved)));
    mS.setAttribute("cx", sx); mS.setAttribute("cy", sy);
    mS.classList.toggle("hidden", isOffZero(id) && p.saved === 0);
    if (p.measured && num(p.measured.value)) { const [mx, my] = polar(C, C, R - 13, dialAngle(tOf(p.measured.value))); mM.setAttribute("d", `M${mx},${my - 4} l-4,7 h8 z`); }
    else mM.setAttribute("d", "");
    vals.replaceChildren(...valueLine(p), ...(isOff ? [el("span", { class: "muted" }, "無効")] : []));
    card.classList.toggle("changed", v !== p.saved);
    reset.disabled = v === p.saved;
  };
  return card;
}

// 図の横に並べる値の表（数値の入力、保存済み、測定、変化、戻す・測定値）
function keyTable(ids) {
  const t = el("table", { class: "keys" });
  ids.forEach((id, i) => {
    const p = byId[id];
    if (!p) return;
    const inp = el("input", { type: "number", step: "any", onchange: (e) => e.target.value !== "" && setEdit(id, +e.target.value, { exact: true }) });
    const cS = el("td", { class: "num s" }), cM = el("td", { class: "num m" }), cD = el("td", { class: "num" });
    const back = el("button", { class: "small ghost", title: "保存済みに戻す", onclick: () => setEdit(id, p.saved) }, "戻す");
    const meas = el("button", { class: "small ghost", title: "測定値にする", onclick: () => p.measured && setEdit(id, p.measured.value, { exact: true }) }, "測定値");
    const tr = el("tr", { class: "keyrow", "data-id": id }, el("td", { class: "kname" }, el("span", { class: `sw k-${i}` }), p.name),
      el("td", {}, inp), cS, cM, cD, el("td", { class: "kbtn" }, back, meas));
    tr._refresh = () => {
      const v = cur(id);
      if (document.activeElement !== inp) inp.value = fmt(v, p);
      cS.textContent = `● ${fmt(p.saved, p)}`;
      cM.textContent = p.measured ? `▲ ${fmt(p.measured.value, p)}` : "";
      cD.replaceChildren(...(v !== p.saved ? [pct(rel(p, p.saved, v))] : []), ...(v !== p.saved && warned(p, p.saved, v) ? [el("span", { class: "warn" }, "⚠")] : []));
      tr.classList.toggle("changed", v !== p.saved);
      back.disabled = v === p.saved;
      meas.disabled = !p.measured || p.measured.value === v;
    };
    t.append(tr);
  });
  return el("div", { class: "keys-wrap" }, t);
}

// o.parts: 部品（見出し・図・値の表・注記）を返す（ほかの図と組み合わせて並べるとき）。o.stack: 図の下に値の表。o.below: 値の表の下に置くもの
function widgetBox(title, sub, fig, ids, note, o = {}) {
  const head = el("div", { class: "wtitle" }, el("strong", {}, title), sub ? el("span", { class: "muted small" }, sub) : null);
  const table = keyTable(ids);
  const noteEl = note ? el("p", { class: "muted small" }, note) : null;
  if (o.parts) return { head, fig, table, note: noteEl };
  return el("div", { class: `widget ${o.stack ? "stack" : ""}` }, head,
    el("div", { class: "wbody" }, el("div", { class: "figbox" }, fig), el("div", { class: "tablecol" }, table, o.below || null)), noteEl);
}

function needMeasure(unit, what) {
  return el("div", { class: "widget hint" }, el("span", { class: "muted" }, `${what}は、見出しの「${UNIT_SHORT[unit]}」で測ると出ます。`));
}

// ---------------------------------------------------------------- 図: 分布（U2・U3・U4・U5）
const DIST_W = {
  pinch: { unit: "pinch", title: "親指-人差し指の距離（U2）", keys: ["click.pinch_on", "click.pinch_off", "click.drag_off"],
    rest: "普段の姿勢", op: "摘まんだ状態", short: ["成立", "解除", "ドラッグ中の解除"], metric: "ti" },
  right: { unit: "right", title: "親指-中指の距離（U3）", keys: ["click.right_on", "click.right_off", "click.right_arm"],
    rest: "普段の姿勢", op: "中指で摘まんだ状態", short: ["成立", "解除", "直前に離れている"], metric: "tm" },
  thumbfold: { unit: "peace", title: "親指の畳み（U4）", keys: ["peace.thumb_fold_on", "peace.thumb_fold_off"],
    rest: "普段の姿勢", op: "ピース", short: ["入る", "抜ける"], metric: "thumb_fold" },
  far: { unit: "thumbsup", title: "親指-手の平の中心（U5）", keys: ["thumbsup.thumbsup_thumb_far_min", "thumbsup.thumbsup_stay_thumb_far_min"],
    rest: "間違えやすいグー", op: "いいね", short: ["入る", "保つ"], metric: "thumb_far" },
};

function distWidget(kind, o = {}) {
  const cfg = DIST_W[kind];
  const d = dists[cfg.unit];
  if (!d || !d.available || !d.op || !d.op.length) return needMeasure(cfg.unit, `${cfg.title}の分布と閾値`);
  const W = 560, H = 172, P = 16, B = 142;
  const svg = newSvg(W, H);
  svg.classList.add("dist");
  keepTextSize(svg, W);
  let lo, hi;
  const all = d.rest.concat(d.op).slice().sort((a, b) => a - b);
  const q = (f) => all[Math.min(all.length - 1, Math.max(0, Math.floor(f * (all.length - 1))))];
  const X = (v) => P + ((v - lo) / (hi - lo)) * (W - 2 * P);
  const V = (x) => lo + ((x - P) / (W - 2 * P)) * (hi - lo);
  const stat = svgEl(svg, "g"), dyn = svgEl(svg, "g");
  function full() {
    const vals = cfg.keys.flatMap((id) => [cur(id), byId[id].saved, byId[id].measured?.value]).filter(num);
    lo = Math.min(q(0.003), ...vals); hi = Math.max(q(0.997), ...vals);
    const pad = (hi - lo) * 0.06 || 0.05; lo -= pad; hi += pad;
    stat.replaceChildren();
    const N = 50, bw = (hi - lo) / N;
    for (const [arr, cls] of [[d.rest, "h-rest"], [d.op, "h-op"]]) {
      const h = new Array(N).fill(0);
      for (const v of arr) { const i = Math.floor((v - lo) / bw); if (i >= 0 && i < N) h[i]++; }
      const m = Math.max(1, ...h);
      h.forEach((c, i) => c && svgEl(stat, "rect", { class: cls, x: X(lo + i * bw), y: B - (c / m) * (B - 44), width: Math.max(1, X(lo + bw) - X(lo) - 1), height: (c / m) * (B - 44) }));
    }
    svgEl(stat, "line", { class: "axis", x1: P, x2: W - P, y1: B, y2: B });
    for (let i = 0; i <= 5; i++) { const v = lo + (i * (hi - lo)) / 5; svgEl(stat, "text", { class: "tick", x: X(v), y: B + 26, "text-anchor": "middle" }, v.toFixed(2)); }
    update(true);
  }
  function update(fromFull) {
    const vs = cfg.keys.map((id) => cur(id));
    if (!fromFull && !svg.classList.contains("dragging") && vs.some((v) => v < lo || v > hi)) return full();
    dyn.replaceChildren();
    cfg.keys.forEach((id, i) => {
      const p = byId[id];
      if (num(p.saved)) svgEl(dyn, "path", { class: "mk-saved", d: `M${X(p.saved)},${B + 2} l-4,7 h8 z` });
      if (p.measured && num(p.measured.value)) svgEl(dyn, "path", { class: "mk-measured", d: `M${X(p.measured.value)},${B + 11} l-4,7 h8 z` });
      const x = X(cur(id));
      svgEl(dyn, "line", { class: `thr k-${i}`, x1: x, x2: x, y1: 8 + i * 12, y2: B });
      svgEl(dyn, "circle", { class: `handle k-${i}`, cx: x, cy: 8 + i * 12, r: 5 });
      svgEl(dyn, "text", { class: `lbl k-${i}`, x: x + 8, y: 12 + i * 12 }, cfg.short[i]);
    });
  }
  draggable(svg, (pt) => {
    let best = null;
    cfg.keys.forEach((id) => { const dx = Math.abs(pt.x - X(cur(id))); if (dx < 10 && (!best || dx < best.dx)) best = { id, dx }; });
    return best;
  }, (o, pt) => setEdit(o.id, V(pt.x)));
  full();
  widgets.push({ keys: cfg.keys, update: () => update(false) });
  const liveG = svgEl(svg, "g");
  live.hooks.push((snap) => {
    liveG.replaceChildren();
    const v = snap && snap.hand ? snap[cfg.metric] : null;
    if (!num(v) || v < lo || v > hi) return;
    svgEl(liveG, "line", { class: "live-line", x1: X(v), x2: X(v), y1: 2, y2: B });
    svgEl(liveG, "text", { class: "live-lbl", x: X(v) - 4, y: B - 4, "text-anchor": "end" }, "今");
  });
  const legend = el("div", { class: "legend" }, el("span", {}, el("span", { class: "sw h-rest" }), `${cfg.rest}（${d.rest.length}）`),
    el("span", {}, el("span", { class: "sw h-op" }), `${cfg.op}（${d.op.length}）`), el("span", { class: "c-saved" }, "▲ 保存済み（上）"),
    el("span", { class: "c-measured" }, "▲ 測定（下）"), el("span", { class: "muted" }, "線をドラッグして変更"));
  return widgetBox(cfg.title, `${d.recording}、${(d.t || "").replace("T", " ")}`, el("div", {}, svg, legend), cfg.keys, null, o);
}

// ---------------------------------------------------------------- 図: 操作範囲（U1）
function roiWidget() {
  const d = dists.range && dists.range.available ? dists.range : null;
  const keys = ["cursor.roi_cx", "cursor.roi_cy", "cursor.roi_width"];
  const [iw, ih] = d ? d.image_size : S.image_size, [sw, sh] = d ? d.screen_size : S.screen_size;
  const W = 480, H = (W * ih) / iw;
  const svg = newSvg(W, H);
  const hOf = (w) => (w * iw * (sh / sw)) / ih; // 操作範囲の高さ（画像の高さに対する比）。画面と同じ縦横比
  const rect = (cx, cy, w) => ({ x: (1 - (cx + w / 2)) * W, y: (cy - hOf(w) / 2) * H, w: w * W, h: hOf(w) * H }); // 利用者から見た向き（鏡像）
  svgEl(svg, "rect", { class: "frame", x: 0, y: 0, width: W, height: H });
  for (const [x, y] of d ? d.points : []) svgEl(svg, "circle", { class: "pt", cx: (1 - x) * W, cy: y * H, r: 1.6 });
  const dyn = svgEl(svg, "g");
  function update() {
    dyn.replaceChildren();
    const draw = (cx, cy, w, cls) => { const r = rect(cx, cy, w); svgEl(dyn, "rect", { class: cls, x: r.x, y: r.y, width: r.w, height: r.h }); return r; };
    draw(...keys.map((k) => byId[k].saved), "roi-saved");
    if (keys.every((k) => byId[k].measured)) draw(...keys.map((k) => byId[k].measured.value), "roi-measured");
    const r = draw(...keys.map(cur), "roi-edit");
    svgEl(dyn, "rect", { class: "handle-sq", x: r.x + r.w - 6, y: r.y + r.h - 6, width: 12, height: 12 });
  }
  draggable(svg, (pt) => {
    const r = rect(...keys.map(cur));
    if (Math.abs(pt.x - (r.x + r.w)) < 12 && Math.abs(pt.y - (r.y + r.h)) < 12) return { kind: "size" };
    if (pt.x >= r.x && pt.x <= r.x + r.w && pt.y >= r.y && pt.y <= r.y + r.h) return { kind: "move", pt, cx: cur(keys[0]), cy: cur(keys[1]) };
    return null;
  }, (o, pt) => {
    if (o.kind === "size") {
      const cxd = (1 - cur(keys[0])) * W, cyd = cur(keys[1]) * H;
      setEdit(keys[2], Math.max((2 * Math.abs(pt.x - cxd)) / W, (2 * Math.abs(pt.y - cyd)) / H / hOf(1)));
    } else {
      setEdit(keys[0], o.cx - (pt.x - o.pt.x) / W);
      setEdit(keys[1], o.cy + (pt.y - o.pt.y) / H);
    }
  });
  update();
  widgets.push({ keys, update });
  const liveG = svgEl(svg, "g");
  live.hooks.push((snap) => {
    liveG.replaceChildren();
    if (snap && snap.hand && snap.palm && num(snap.palm[0])) svgEl(liveG, "circle", { class: "live-dot", cx: (1 - snap.palm[0]) * W, cy: snap.palm[1] * H, r: 6 });
  });
  const legend = el("div", { class: "legend" }, d ? el("span", {}, el("span", { class: "sw pt" }), `手のひら中心（${d.points.length}）`)
    : el("span", { class: "muted" }, "U1 を測ると、届いた手のひら中心の点が出ます"),
    el("span", { class: "c-saved" }, "‐‐ 保存済み"), el("span", { class: "c-measured" }, "‐‐ 測定"), el("span", { class: "c-edit" }, "― 変更中"),
    el("span", { class: "muted" }, "四角を動かす、右下の角で大きさ"));
  return widgetBox("操作範囲", d ? `カメラの画像（利用者から見た向き）。点は ${d.recording}` : "カメラの画像（利用者から見た向き）", el("div", {}, svg, legend), keys,
    "幅が大きいほど、同じ手の動きでカーソルが動く量は小さくなります。高さは画面の縦横比で決まります。");
}

// ---------------------------------------------------------------- 図: 円（ピース、いいね）
function polar(cx, cy, r, deg) { const a = (deg * Math.PI) / 180; return [cx + r * Math.cos(a), cy - r * Math.sin(a)]; }
function arcPath(cx, cy, r, a0, a1) { const [x0, y0] = polar(cx, cy, r, a0), [x1, y1] = polar(cx, cy, r, a1); return `M${x0},${y0} A${r},${r} 0 0 0 ${x1},${y1}`; }
function sectorPath(cx, cy, r, a0, a1) { const [x0, y0] = polar(cx, cy, r, a0), [x1, y1] = polar(cx, cy, r, a1); return `M${cx},${cy} L${x0},${y0} A${r},${r} 0 0 0 ${x1},${y1} Z`; }
const SECTOR = { right: [-45, 45], up: [45, 135], left: [135, 225], down: [225, 315] };
function angleOf(cx, cy, pt) { let a = (Math.atan2(cy - pt.y, pt.x - cx) * 180) / Math.PI; if (a < -45) a += 360; return a; }
function sectorOf(a) { return Object.entries(SECTOR).find(([, [a0, a1]]) => a >= a0 && a < a1)?.[0] || "right"; }
function diagonals(g, C, r) {
  for (const deg of [45, 135]) { const [x0, y0] = polar(C, C, r, deg), [x1, y1] = polar(C, C, r, deg + 180); svgEl(g, "line", { class: "diag", x1: x0, y1: y0, x2: x1, y2: y1 }); }
}

function peaceDialWidget(o = {}) {
  const keys = ["peace.peace_dead_disp", "peace.peace_fire_disp", "peace.peace_max_disp"];
  const W = 300, C = W / 2, R = 136;
  let SCALE = 1;
  const svg = newSvg(W, W, true);
  const dyn = svgEl(svg, "g");
  function update() {
    // 縮尺は外周に合わせる（ドラッグ中は変えない）
    if (!svg.classList.contains("dragging")) SCALE = R / Math.max(1.0, 1.12 * Math.max(cur(keys[2]), byId[keys[2]].saved));
    dyn.replaceChildren();
    const [dead, fire, max] = keys.map(cur);
    svgEl(dyn, "circle", { class: "dial-bg", cx: C, cy: C, r: max * SCALE });
    for (const s of ["left", "right"]) svgEl(dyn, "path", { class: "nav-sector", d: sectorPath(C, C, max * SCALE, ...SECTOR[s]) });
    diagonals(dyn, C, max * SCALE);
    const arcs = (id, dirs, cls, r) => { for (const dir of dirs) svgEl(dyn, "path", { class: cls, d: arcPath(C, C, r, SECTOR[dir][0] + 3, SECTOR[dir][1] - 3) }); };
    arcs(keys[0], ["up", "down"], "arc-saved", byId[keys[0]].saved * SCALE);
    arcs(keys[2], ["up", "down"], "arc-saved", byId[keys[2]].saved * SCALE);
    arcs(keys[1], ["left", "right"], "arc-saved", byId[keys[1]].saved * SCALE);
    arcs(keys[0], ["up", "down"], "arc-edit k-0", dead * SCALE);
    arcs(keys[2], ["up", "down"], "arc-edit k-2", max * SCALE);
    arcs(keys[1], ["left", "right"], "arc-fire k-1", fire * SCALE);
    svgEl(dyn, "text", { class: "lbl", x: C, y: C - max * SCALE - 6, "text-anchor": "middle" }, "スクロール最大");
    svgEl(dyn, "text", { class: "lbl", x: C, y: C - dead * SCALE + 13, "text-anchor": "middle" }, "不感帯");
    // 左右の動作（nav_swap が true: 手を左へ → 進む、右へ → 戻る）
    const swap = !!cur("peace.nav_swap");
    svgEl(dyn, "text", { class: "lbl", x: C + fire * SCALE + 4, y: C + 4 }, swap ? "戻る" : "進む");
    svgEl(dyn, "text", { class: "lbl", x: C - fire * SCALE - 4, y: C + 4, "text-anchor": "end" }, swap ? "進む" : "戻る");
  }
  draggable(svg, (pt) => {
    const r = Math.hypot(pt.x - C, pt.y - C) / SCALE, sec = sectorOf(angleOf(C, C, pt));
    const cands = sec === "up" || sec === "down" ? [keys[0], keys[2]] : [keys[1]]; // 上下は不感帯と外周、左右は戻る・進む
    let best = null;
    cands.forEach((id) => { const d = Math.abs(r - cur(id)) * SCALE; if (d < 12 && (!best || d < best.d)) best = { id, d }; });
    return best;
  }, (o, pt) => {
    let v = Math.hypot(pt.x - C, pt.y - C) / SCALE;
    const [dead, fire, max] = keys.map(cur);
    if (o.id === keys[0]) v = Math.min(v, fire - 0.05, max - 0.05); // 不感帯 < 戻る・進む、外周
    if (o.id === keys[1]) v = Math.max(v, dead + 0.05);
    if (o.id === keys[2]) v = Math.max(v, dead + 0.05);
    setEdit(o.id, v);
  });
  update();
  widgets.push({ keys: [...keys, "peace.nav_swap"], update });
  const liveG = svgEl(svg, "g");
  live.hooks.push((snap) => {
    liveG.replaceChildren();
    if (snap && snap.peace) svgEl(liveG, "circle", { class: "live-dot", cx: C + Math.max(-C, Math.min(C, snap.peace[0] * SCALE)), cy: C - Math.max(-C, Math.min(C, snap.peace[1] * SCALE)), r: 6 });
  });
  return widgetBox("ピースの領域", "run のピースモードの円と同じ。上下 = スクロール、左右 = 戻る・進む", svg, keys,
    "単位は手の大きさ（画像上の手首→中指付け根）。弧をドラッグして変更。上下: 不感帯の端から外周までで、スクロールが最大速度まで速くなる。"
    + "左右: 弧を越えて止まると戻る・進む。不感帯は 4 方向に共通の「何もしない」範囲なので、戻る・進むの弧より内側にしかできない。", o);
}

function radarWidget() {
  const keys = ["thumbsup.thumbsup_dead_disp", "thumbsup.thumbsup_rearm_disp", "thumbsup.thumbsup_fire_up",
    "thumbsup.thumbsup_fire_left", "thumbsup.thumbsup_fire_right", "thumbsup.thumbsup_max_disp"];
  const fireOf = { up: keys[2], left: keys[3], right: keys[4] };
  const LIM = 3.2, W = 340, C = W / 2, R = 156, SCALE = R / LIM;
  const svg = newSvg(W, W, true);
  const d = dists.thumbsup;
  const stat = svgEl(svg, "g"), dyn = svgEl(svg, "g");
  if (d && d.available && d.points) for (const p of d.points) {
    const r = Math.hypot(p.x, p.y), k = r > LIM ? LIM / r : 1;
    svgEl(stat, "circle", { class: `pt l-${p.label}`, cx: C + p.x * k * SCALE, cy: C - p.y * k * SCALE, r: 1.6 });
  }
  function update() {
    dyn.replaceChildren();
    const v = Object.fromEntries(keys.map((k) => [k, cur(k)]));
    const max = v[keys[5]];
    svgEl(dyn, "circle", { class: "ring-saved", cx: C, cy: C, r: byId[keys[5]].saved * SCALE });
    svgEl(dyn, "circle", { class: "ring-max k-5", cx: C, cy: C, r: max * SCALE });
    svgEl(dyn, "path", { class: "down-sector", d: sectorPath(C, C, max * SCALE, ...SECTOR.down) });
    diagonals(dyn, C, max * SCALE);
    for (const [dir, id] of Object.entries(fireOf)) {
      const [a0, a1] = SECTOR[dir];
      svgEl(dyn, "path", { class: "arc-saved", d: arcPath(C, C, byId[id].saved * SCALE, a0 + 4, a1 - 4) });
      svgEl(dyn, "path", { class: `arc-fire k-${keys.indexOf(id)}`, d: arcPath(C, C, v[id] * SCALE, a0 + 4, a1 - 4) });
      const [lx, ly] = polar(C, C, v[id] * SCALE + 11, (a0 + a1) / 2);
      svgEl(dyn, "text", { class: "lbl", x: lx, y: ly + 4, "text-anchor": "middle" }, { up: "上", left: "左", right: "右" }[dir]);
    }
    svgEl(dyn, "circle", { class: "ring-saved", cx: C, cy: C, r: byId[keys[0]].saved * SCALE });
    svgEl(dyn, "circle", { class: "ring-dead k-0", cx: C, cy: C, r: v[keys[0]] * SCALE });
    svgEl(dyn, "circle", { class: "ring-rearm k-1", cx: C, cy: C, r: v[keys[1]] * SCALE });
    svgEl(dyn, "text", { class: "lbl", x: C, y: C + max * SCALE - 8, "text-anchor": "middle" }, "下（使わない）");
  }
  draggable(svg, (pt) => {
    const r = Math.hypot(pt.x - C, pt.y - C) / SCALE, sec = sectorOf(angleOf(C, C, pt));
    const cands = [keys[0], keys[1], keys[5]];
    if (fireOf[sec]) cands.push(fireOf[sec]);
    let best = null;
    for (const id of cands) { const dd = Math.abs(r - cur(id)) * SCALE; if (dd < 10 && (!best || dd < best.d)) best = { id, d: dd }; }
    return best;
  }, (o, pt) => setEdit(o.id, Math.hypot(pt.x - C, pt.y - C) / SCALE));
  update();
  widgets.push({ keys, update });
  const liveG = svgEl(svg, "g");
  live.hooks.push((snap) => {
    liveG.replaceChildren();
    if (!snap || !snap.thumbsup) return;
    const [x, y] = snap.thumbsup, r = Math.hypot(x, y), k = r > LIM ? LIM / r : 1;
    svgEl(liveG, "circle", { class: "live-dot", cx: C + x * k * SCALE, cy: C - y * k * SCALE, r: 6 });
  });
  const legend = el("div", { class: "legend" },
    d && d.available ? el("span", { class: "muted" }, `点: ${d.recording} の各方向（`, el("span", { class: "c-up" }, "上"), "・", el("span", { class: "c-left" }, "左"), "・",
      el("span", { class: "c-right" }, "右"), "・", el("span", { class: "c-down" }, "下"), "・正面）") : el("span", { class: "muted" }, "点: U5 を測ると、各方向の位置が出ます"),
    el("span", { class: "muted" }, "円・弧をドラッグして変更"));
  return widgetBox("いいねの領域", "run のいいねモードの円と同じ。中央の円が不感帯、細い破線が再武装、弧が発火", el("div", {}, svg, legend), keys,
    "単位は手の大きさ。各方向の弧を越えて止まると発火し、再武装の円より中央へ戻ると次を許す。外周は表示の大きさだけ。");
}

// ---------------------------------------------------------------- 図: ズームの棒
function zoomWidget(o = {}) {
  const keys = ["zoom.zoom_dead", "zoom.zoom_fire_level", "zoom.zoom_max"];
  const W = 300, H = 300, C = H / 2, BX = 90, BW = 36;
  const svg = newSvg(W, H, true);
  const d = dists.zoom;
  let vmax = 0;
  const stat = svgEl(svg, "g"), dyn = svgEl(svg, "g");
  const Y = (v) => C - (v / vmax) * (C - 16);
  const Vof = (y) => ((C - y) / (C - 16)) * vmax;
  function full() {
    const pts = d && d.available && d.points ? d.points : [];
    const absv = pts.map((p) => Math.abs(p.v)).sort((a, b) => a - b);
    const mx = byId["zoom.zoom_max"];
    vmax = Math.max(1.25 * Math.max(...[cur(mx.id), mx.saved, mx.measured?.value].filter(num)), absv.length ? absv[Math.floor(absv.length * 0.99)] * 1.05 : 0);
    stat.replaceChildren();
    svgEl(stat, "rect", { class: "bar-bg", x: BX, y: 16, width: BW, height: H - 32 });
    svgEl(stat, "line", { class: "axis", x1: BX - 6, x2: BX + BW + 6, y1: C, y2: C });
    svgEl(stat, "text", { class: "tick", x: BX + BW / 2, y: 11, "text-anchor": "middle" }, "イン");
    svgEl(stat, "text", { class: "tick", x: BX + BW / 2, y: H - 3, "text-anchor": "middle" }, "アウト");
    pts.forEach((p, i) => svgEl(stat, "circle", { class: `pt l-${p.label}`, cx: BX + BW + 20 + ((i * 37) % 100), cy: Y(Math.max(-vmax, Math.min(vmax, p.v))), r: 1.6 }));
    update(true);
  }
  function update(fromFull) {
    const [dead, fl, max] = keys.map(cur);
    if (!fromFull && !svg.classList.contains("dragging") && max * 1.02 > vmax) return full();
    dyn.replaceChildren();
    const fire = dead + fl * (max - dead);
    svgEl(dyn, "rect", { class: "dead-band k-0", x: BX, y: Y(dead), width: BW, height: Y(-dead) - Y(dead) });
    const sMax = byId[keys[2]].saved;
    for (const s of [1, -1]) {
      svgEl(dyn, "line", { class: "thr-saved", x1: BX - 12, x2: BX - 2, y1: Y(s * sMax), y2: Y(s * sMax) });
      svgEl(dyn, "line", { class: "thr k-2", x1: BX - 8, x2: BX + BW + 8, y1: Y(s * max), y2: Y(s * max) });
      svgEl(dyn, "line", { class: "thr dashed k-1", x1: BX - 8, x2: BX + BW + 8, y1: Y(s * fire), y2: Y(s * fire) });
    }
    svgEl(dyn, "text", { class: "lbl", x: BX - 16, y: Y(max) + 4, "text-anchor": "end" }, "外端");
    svgEl(dyn, "text", { class: "lbl", x: BX - 16, y: Y(fire) + 4, "text-anchor": "end" }, "発火");
    svgEl(dyn, "text", { class: "lbl", x: BX - 16, y: Y(dead) + 4, "text-anchor": "end" }, "不感帯");
  }
  draggable(svg, (pt) => {
    const v = Math.abs(Vof(pt.y));
    const [dead, fl, max] = keys.map(cur);
    const cand = { [keys[0]]: dead, [keys[1]]: dead + fl * (max - dead), [keys[2]]: max };
    let best = null;
    for (const [id, x] of Object.entries(cand)) { const dd = Math.abs(Y(v) - Y(x)); if (dd < 9 && (!best || dd < best.d)) best = { id, d: dd }; }
    return best;
  }, (o, pt) => {
    const v = Math.abs(Vof(pt.y));
    const [dead, , max] = keys.map(cur);
    if (o.id === keys[1]) setEdit(o.id, (v - dead) / (max - dead));
    else if (o.id === keys[0]) setEdit(o.id, Math.min(v, max - 0.005));
    else setEdit(o.id, Math.max(v, dead + 0.005));
  });
  full();
  widgets.push({ keys, update: () => update(false) });
  const liveG = svgEl(svg, "g");
  live.hooks.push((snap) => {
    liveG.replaceChildren();
    if (snap && num(snap.zoom)) svgEl(liveG, "line", { class: "live-line", x1: BX - 4, x2: BX + BW + 4, y1: Y(Math.max(-vmax, Math.min(vmax, snap.zoom))), y2: Y(Math.max(-vmax, Math.min(vmax, snap.zoom))) });
  });

  const legend = el("div", { class: "legend" },
    d && d.available ? el("span", { class: "muted" }, `点: ${d.recording}（`, el("span", { class: "c-in" }, "イン"), "・", el("span", { class: "c-out" }, "アウト"), "・正面）") : null,
    el("span", { class: "muted" }, "線をドラッグして変更"));
  // 符号: +1 = 指先を手前（カメラから遠ざける、手招き）でイン。-1 = 画面側（カメラへ近づける）でイン（depth のとき）
  const dir = segmented("zoom.zoom_sign", [[1.0, "手前"], [-1.0, "画面側"]]);
  widgets.push({ keys: ["zoom.zoom_sign"], update: () => dir._refresh() });
  const signNote = el("span", { class: "muted" });
  widgets.push({ keys: ["zoom.zoom_signal"], update: () => { signNote.textContent = cur("zoom.zoom_signal") === "length" ? "（length では向きを区別できない）" : ""; } });
  const sig = segmented("zoom.zoom_signal", [["depth", "奥行き（depth）"], ["curl", "指の曲がり（curl）"], ["length", "指の長さ（length）"]]);
  widgets.push({ keys: ["zoom.zoom_signal"], update: () => sig._refresh() });
  const extra = el("div", { class: "zoom-opts small" },
    el("div", { class: "row wrap" }, "ズームインの方向", dir, signNote),
    el("div", { class: "row wrap" }, "測り方", sig, el("span", { class: "muted" }, "（変えたら、不感帯と外端も測り直す）")));
  return widgetBox("ズームの範囲", "run のズームの棒と同じ。上がイン、下がアウト", el("div", {}, svg, legend, extra), keys,
    "発火は、不感帯の端（0）から外端（1）までの割合。そこを越えて止まると 1 段送る。", o);
}

// ---------------------------------------------------------------- 図: ピースの中立方向（U4）
function neutralWidget(o = {}) {
  const keys = ["peace.peace_neutral_yaw_deg", "peace.peace_neutral_pitch_deg"];
  const d = dists.peace;
  if (!d || !d.available || !d.neutral || !d.neutral.length) return o.parts ? { hint: needMeasure("peace", "ピースの中立方向") } : needMeasure("peace", "ピースの中立方向");
  const W = 300, C = W / 2, R = 136, SCALE = R / 90;
  const svg = newSvg(W, W, true);
  for (const r of [15, 30, 45, 60, 75, 90]) svgEl(svg, "circle", { class: r % 45 ? "grid" : "grid strong", cx: C, cy: C, r: r * SCALE });
  svgEl(svg, "line", { class: "grid strong", x1: C - R, x2: C + R, y1: C, y2: C });
  svgEl(svg, "line", { class: "grid strong", x1: C, x2: C, y1: C - R, y2: C + R });
  for (const r of [45, 90]) svgEl(svg, "text", { class: "tick", x: C + r * SCALE - 2, y: C - 3, "text-anchor": "end" }, `${r}°`);
  svgEl(svg, "text", { class: "tick", x: C + 4, y: 12 }, "上");
  svgEl(svg, "text", { class: "tick", x: W - 14, y: C + 13 }, "右");
  for (const [yaw, pitch] of d.neutral) svgEl(svg, "circle", { class: "pt", cx: C + yaw * SCALE, cy: C - pitch * SCALE, r: 1.6 });
  const dyn = svgEl(svg, "g");
  function update() {
    dyn.replaceChildren();
    const [sy, sp] = keys.map((k) => byId[k].saved);
    svgEl(dyn, "circle", { class: "dot-saved", cx: C + sy * SCALE, cy: C - sp * SCALE, r: 5 });
    if (keys.every((k) => byId[k].measured)) {
      const [my, mp] = keys.map((k) => byId[k].measured.value);
      svgEl(dyn, "path", { class: "mk-measured", d: `M${C + my * SCALE},${C - mp * SCALE - 6} l-5,9 h10 z` });
    }
    svgEl(dyn, "circle", { class: "handle k-0", cx: C + cur(keys[0]) * SCALE, cy: C - cur(keys[1]) * SCALE, r: 6 });
  }
  draggable(svg, (pt) => (Math.hypot(pt.x - (C + cur(keys[0]) * SCALE), pt.y - (C - cur(keys[1]) * SCALE)) < 12 ? {} : null),
    (o, pt) => { setEdit(keys[0], (pt.x - C) / SCALE); setEdit(keys[1], (C - pt.y) / SCALE); });
  update();
  widgets.push({ keys, update });
  return widgetBox("ピースの中立方向（U4）", `${d.recording}。方位角（右が正）・仰角（上が正）`, el("div", {}, svg,
    el("div", { class: "legend" }, el("span", {}, el("span", { class: "sw pt" }), `ピースの指の向き（${d.neutral.length}）`),
      el("span", { class: "c-saved" }, "● 保存済み"), el("span", { class: "c-measured" }, "▲ 測定"), el("span", { class: "muted" }, "青い点をドラッグ"))), keys,
    "今の方式（指先の変位）では使われません。角度の方式に戻すときのための値です。", o);
}

// ---------------------------------------------------------------- 図: スクロールの速さの曲線
// 速さ = 最大速度 × u^指数（u = 不感帯の端 0 → 外周 1 の位置。peace.py）。右端の点で最大速度、中ほどの点で曲がり具合
function scrollCurveWidget(o = {}) {
  // 縦軸がピースの変位（下: 不感帯の端 0 → 上: 外周 1）、横軸が速さ（px/秒）
  const keys = ["peace.scroll_max_speed", "peace.scroll_exponent"];
  const W = 300, H = 340, L = 30, R = 20, T = 22, B = 40;
  const svg = newSvg(W, H, true);
  let vmax = 1;
  const X = (v) => L + (v / vmax) * (W - L - R);
  const Y = (u) => H - B - u * (H - T - B);
  const dyn = svgEl(svg, "g");
  const curve = (mx, e) => Array.from({ length: 41 }, (_, i) => { const u = i / 40; return `${i ? "L" : "M"}${X(mx * u ** e)},${Y(u)}`; }).join(" ");
  function update() {
    const [mx, e] = keys.map(cur);
    if (!svg.classList.contains("dragging")) vmax = Math.max(1000, 1.25 * Math.max(mx, byId[keys[0]].saved));
    dyn.replaceChildren();
    for (let i = 0; i <= 4; i++) {
      const v = (vmax * i) / 4;
      svgEl(dyn, "line", { class: "grid", x1: X(v), x2: X(v), y1: Y(0), y2: Y(1) });
      svgEl(dyn, "text", { class: "tick", x: X(v), y: H - B + 13, "text-anchor": "middle" }, Math.round(v));
    }
    svgEl(dyn, "line", { class: "axis", x1: X(0), x2: X(0), y1: Y(0), y2: Y(1) });
    svgEl(dyn, "text", { class: "tick", x: X(0) + 4, y: Y(1) - 8 }, "外周（最大）");
    svgEl(dyn, "text", { class: "tick", x: X(0) + 4, y: Y(0) - 4 }, "不感帯の端");
    svgEl(dyn, "text", { class: "tick", x: (L + W - R) / 2, y: H - 6, "text-anchor": "middle" }, "スクロールの速さ（px/秒）");
    svgEl(dyn, "text", { class: "tick", x: 14, y: (T + H - B) / 2, transform: `rotate(-90 14 ${(T + H - B) / 2})`, "text-anchor": "middle" }, "ピースの変位");
    svgEl(dyn, "path", { class: "curve-saved", d: curve(byId[keys[0]].saved, byId[keys[1]].saved) });
    svgEl(dyn, "path", { class: "curve k-0", d: curve(mx, e) });
    svgEl(dyn, "circle", { class: "handle k-0", cx: X(mx), cy: Y(1), r: 6 });
    svgEl(dyn, "circle", { class: "handle k-1", cx: X(mx * 0.5 ** e), cy: Y(0.5), r: 6 });
    svgEl(dyn, "text", { class: "lbl", x: X(mx) - 8, y: Y(1) - 8, "text-anchor": "end" }, "最大速度");
    svgEl(dyn, "text", { class: "lbl", x: X(mx * 0.5 ** e) + 9, y: Y(0.5) + 4 }, "曲がり具合");
  }
  draggable(svg, (pt) => {
    const [mx, e] = keys.map(cur);
    if (Math.hypot(pt.x - X(mx), pt.y - Y(1)) < 12) return { id: keys[0] };
    if (Math.hypot(pt.x - X(mx * 0.5 ** e), pt.y - Y(0.5)) < 12) return { id: keys[1] };
    return null;
  }, (h, pt) => {
    const v = ((pt.x - L) / (W - L - R)) * vmax;
    if (h.id === keys[0]) setEdit(h.id, v);
    else { const r = Math.min(0.99, Math.max(1e-3, v / cur(keys[0]))); setEdit(h.id, Math.log(r) / Math.log(0.5)); } // 0.5^指数 = r
  });
  update();
  widgets.push({ keys, update });
  const liveG = svgEl(svg, "g");
  live.hooks.push((snap) => {
    liveG.replaceChildren();
    if (!snap || !snap.peace) return;
    const [sx, sy] = snap.peace, ax = Math.abs(sx), ay = Math.abs(sy);
    const dead = cur("peace.peace_dead_disp"), max = cur("peace.peace_max_disp");
    // peace.py と同じ: 上下の領域（|y| ≥ |x| かつ不感帯の外）のときだけスクロールする
    const u = ay >= ax && Math.max(ax, ay) >= dead ? Math.min(1, (ay - dead) / Math.max(max - dead, 1e-9)) : 0;
    const v = cur(keys[0]) * u ** cur(keys[1]);
    svgEl(liveG, "line", { class: "live-line", x1: X(0), x2: X(v), y1: Y(u), y2: Y(u) });
    svgEl(liveG, "circle", { class: "live-dot", cx: X(v), cy: Y(u), r: 6 });
    svgEl(liveG, "text", { class: "live-lbl", x: X(v) + 8, y: Y(u) - 8 }, `${Math.round(v)} px/秒 ${sy >= 0 ? "↑" : "↓"}`);
  });
  return widgetBox("スクロールの速さ", "上下の変位に対する速さ（run と同じ式）", el("div", {}, svg,
    el("div", { class: "legend" }, el("span", { class: "c-saved" }, "‐‐ 保存済み"), el("span", { class: "c-edit" }, "― 変更中"),
      el("span", { class: "muted" }, "上の点で最大速度、中ほどの点で曲がり具合（左へ寄せるほど中央付近が遅い）"))), keys, null, { stack: true, ...o });
}

// ピースの領域（左の円）と中立方向（右の円）を横に並べ、真ん中の上に領域の値、下に中立方向の値
function peaceRowWidget() {
  const a = peaceDialWidget({ parts: true });
  const n = neutralWidget({ parts: true });
  const center = el("div", { class: "pr-center" }, el("div", { class: "pr-cap" }, "ピースの領域"), a.table,
    n.hint ? null : el("div", { class: "pr-cap" }, "ピースの中立方向（U4）"), n.hint ? null : n.table);
  const right = n.hint ? el("div", { class: "pr-side" }, n.hint) : el("div", { class: "pr-side" }, n.head, n.fig);
  return el("div", { class: "widget peace-row" },
    el("div", { class: "pr-grid" }, el("div", { class: "pr-side" }, a.head, a.fig), center, right),
    a.note, n.hint ? null : n.note);
}

const WIDGETS = { peaceRow: peaceRowWidget, scrollCurve: scrollCurveWidget, roi: roiWidget, pinch: (o) => distWidget("pinch", o),
  right: (o) => distWidget("right", o), thumbfold: (o) => distWidget("thumbfold", o), far: (o) => distWidget("far", o),
  radar: radarWidget, zoom: zoomWidget }; // ピースの領域と中立方向は peaceRow の中で使う
// 図の中で変更する項目（測定するまで図が出ないものも含む）
const IN_WIDGETS = ["cursor.roi_cx", "cursor.roi_cy", "cursor.roi_width", "peace.peace_neutral_yaw_deg", "peace.peace_neutral_pitch_deg",
  "zoom.zoom_signal", "zoom.zoom_sign", ...Object.values(DIST_W).flatMap((c) => c.keys)];

// ---------------------------------------------------------------- 画面を組む
// 項目（文字列）・小見出しのグループ・図・横並び・縦並びを描く
function renderItem(it, placed) {
  if (typeof it === "string") { placed.add(it); return isTime(byId[it]) ? dialCard(it) : itemRow(it); }
  if (it.row) return el("div", { class: `lrow ${it.cls || ""}` }, it.row.map((x) => renderItem(x, placed)));
  if (it.col) return el("div", { class: "lcol" }, it.col.map((x) => renderItem(x, placed)));
  if (it.g) {
    GROUPS[it.g].items.forEach(([id]) => placed.add(id));
    if (GROUPS[it.g].toggle) placed.add(GROUPS[it.g].toggle);
    return groupBox(it.g);
  }
  const o = { ...it };
  if (it.belowDial) { placed.add(it.belowDial); o.below = dialCard(it.belowDial); }
  if (it.belowLayout) o.below = renderItem(it.belowLayout, placed); // 値の表の下に置くもの
  return WIDGETS[it.w](o);
}

function renderSections() {
  widgets.length = 0;
  live.hooks.length = 0;
  toggleOf = {};
  for (const [t, members] of Object.entries(S.toggles)) for (const m of members) toggleOf[m] = t;
  const root = document.getElementById("sections");
  root.replaceChildren();
  const placed = new Set(IN_WIDGETS);
  for (const entry of LAYOUT) {
    if (entry.pair) root.append(el("div", { class: "panel-row" }, entry.pair.map((sec) => renderSection(sec, placed))));
    else root.append(renderSection(entry, placed));
  }
  for (const w of widgets) w.keys.forEach((k) => placed.add(k));
  const missing = S.params.map((p) => p.id).filter((id) => !placed.has(id));
  if (missing.length) console.warn("画面に置いていない項目", missing);
  refreshAll();
}

function renderSection(sec, placed) {
  const head = el("div", { class: "phead" }, el("h2", {}, sec.title));
  const panel = el("section", { class: "panel" }, head);
  if (sec.toggle) {
    placed.add(sec.toggle);
    const sw = toggleSwitch(!!cur(sec.toggle), (on) => setEdit(sec.toggle, on), `${sec.title}を有効にする`);
    head.append(el("span", { class: "spacer" }), sw);
    widgets.push({ keys: [sec.toggle], update: () => sw._set(!!cur(sec.toggle)) });
  }
  if (sec.units) {
    const ub = el("span", { class: "unit-btns" }, sec.units.map((u) => el("button", { class: "unit-btn", title: `${UNITS.find((x) => x[0] === u)[1]}を測る`,
      onclick: () => measure([u]) }, UNIT_SHORT[u])));
    head.querySelector("h2").after(ub);
  }
  const body = el("div", { class: "pbody", "data-group": sec.toggle || "" });
  let dials = null;
  for (const it of sec.items) {
    if (typeof it === "string" && isTime(byId[it])) { // 続く時間の項目は、ダイヤルを横に並べる
      placed.add(it);
      if (!dials) { dials = el("div", { class: "dials" }); body.append(dials); }
      dials.append(dialCard(it));
      continue;
    }
    dials = null;
    body.append(renderItem(it, placed));
  }
  panel.append(body);
  if (sec.flex) panel.classList.add(`flex-${sec.flex}`);
  return panel;
}

// ---------------------------------------------------------------- 上のバー、候補、履歴
let evalRunning = false;

function refreshTop() {
  const ids = changedIds();
  const nWarn = ids.filter((id) => warned(byId[id], byId[id].saved, edits[id])).length;
  document.getElementById("dirty").textContent = ids.length ? `変更中 ${ids.length} 項目` + (nWarn ? `（警告 ${nWarn}）` : "") : "";
  document.getElementById("btn-save").disabled = !ids.length;
  document.getElementById("btn-save").textContent = ids.length ? `変更を確定（${ids.length}）` : "変更を確定";
  document.getElementById("btn-discard").disabled = !ids.length;
  document.getElementById("btn-eval-edit").disabled = !ids.length || evalRunning;
  document.getElementById("btn-eval-measured").disabled = evalRunning;
  const chip = document.getElementById("run-chip");
  chip.textContent = S.run.length ? `run 動作中（${S.run.join(", ")}）` : "run 停止中";
  chip.classList.toggle("on", S.run.length > 0);
}

function measuredDiffs() { return S.params.filter((p) => p.measured && p.measured.value !== p.saved); }

function renderCandidates() {
  const box = document.getElementById("candidates");
  const rows = measuredDiffs();
  const units = Object.entries(S.units);
  const kids = [];
  if (!units.length) kids.push(el("p", { class: "muted" }, "まだ測定の結果がありません。"));
  for (const [u, r] of units) {
    const title = (UNITS.find((x) => x[0] === u) || [u, u])[1];
    kids.push(el("div", { class: "list-row" }, el("strong", {}, title), el("span", { class: "muted grow" }, (r.t || "").replace("T", " ")),
      el("span", {}, r.candidate ? "候補あり" : "候補なし")));
    for (const w of r.warnings || []) kids.push(el("div", { class: "warn" }, `⚠ ${w}`));
  }
  if (rows.length) {
    const nWarn = rows.filter((p) => warned(p, p.saved, p.measured.value)).length;
    kids.push(el("p", { class: "muted small" }, `保存済みと違う測定の値: ${rows.length} 項目（警告 ${nWarn} 件）。各図の ▲ が測定の値です。`),
      el("button", { onclick: async () => {
        const ok = await confirmBox("測定の値を一括で変更中にする", [el("p", {}, `${rows.length} 項目を、測定の値にします（まだ確定しません）。`),
          nWarn ? el("p", { class: "warn" }, `保存済みから ${Math.round(WARN * 100)}% 以上動く項目が ${nWarn} 件あります。`) : null], "変更中にする");
        if (ok) for (const p of rows) setEdit(p.id, p.measured.value, { exact: true });
      } }, `一括採用（${rows.length} 項目、警告 ${nWarn} 件）`));
  } else if (units.length) kids.push(el("p", { class: "muted" }, "測定の値は、すべて保存済みと同じです。"));
  box.replaceChildren(...kids);
}

// 同じ中身の設定には同じ名前を出す（名前を付けた状態があればその名前、無ければその中身が最初に現れた版の名前。サーバーが付ける）
function renderHistory() {
  const applied = (key) => key === S.current;
  const row = (name, sub, key, ident) => el("div", { class: `list-row ${applied(key) ? "applied" : ""}` },
    el("span", { class: "hname" }, name), el("span", { class: "muted small grow" }, sub),
    applied(key) ? el("span", { class: "chip on" }, "適用中")
      : el("button", { class: "small", onclick: () => restore(ident, `「${name}」`) }, "この設定を適用"));
  document.getElementById("named").replaceChildren(...(S.named.length ? S.named.map((n) => row(n.name, "", n.key, n.name))
    : [el("p", { class: "muted" }, "なし")]));
  document.getElementById("history").replaceChildren(...(S.history.length ? S.history.map((h) => row(h.name, `${h.id}  ${h.route}`, h.key, h.id))
    : [el("p", { class: "muted" }, "まだ保存していません。")]));
  document.getElementById("btn-undo").disabled = S.history.length < 2;
}

// ---------------------------------------------------------------- 終了（13.52）
let runTimer = null;

async function quit() {
  const n = changedIds().length;
  const body = [el("p", {}, "設定画面のサーバーを止めます。run は止めません（つながっていれば設定モードを抜け、操作に戻ります）。"),
    n ? el("p", {}, `変更中の ${n} 項目は確定せずに捨てます。`) : null,
    el("p", { class: "muted" }, "もう一度開くには、オーバーレイのギアのボタンか python -m aircontroller settings。")];
  if (!(await confirmBox("設定画面を終了する", body, n ? "捨てて終了" : "終了"))) return;
  try {
    let r = await api("/api/shutdown", { force: false });
    if (r.busy) {
      const what = r.busy.map((k) => ({ measure: "測定", eval: "見積もり" }[k] || k)).join("・");
      const ok = await confirmBox(`${what}の途中です`, [
        r.busy.includes("eval") ? el("p", {}, "見積もりは止めます（結果は出ません）。") : null,
        r.busy.includes("measure") ? el("p", {}, "測定（calibrate）は最後まで動き、候補は calibration-history/ に残ります。画面には出ません。") : null],
      "それでも終了");
      if (!ok) return;
      r = await api("/api/shutdown", { force: true });
    }
  } catch (e) { return toast(`終了できませんでした: ${e.message}`); }
  for (const k of Object.keys(edits)) delete edits[k]; // run には送らない（サーバーが止まると run は保存済みの値に戻る）
  clearInterval(runTimer);
  if (live.es) { live.es.close(); live.es = null; }
  document.body.replaceChildren(el("main", {}, el("section", { class: "panel" }, el("h2", {}, "設定画面を終了しました"),
    el("p", { class: "muted" }, "このタブは閉じてかまいません。もう一度開くには、オーバーレイのギアのボタンか python -m aircontroller settings。"))));
  window.close(); // スクリプトで開いたタブでなければ閉じない（そのときは上の案内が残る）
}

function clearEdits() { for (const k of Object.keys(edits)) delete edits[k]; sendLiveConfig(); }

async function restore(id, label) {
  if (changedIds().length && !(await confirmBox("変更中の値があります", el("p", {}, "戻すと、変更中の値は捨てます。"), "捨てて続ける"))) return;
  if (!(await confirmBox(`${label}の設定を適用する`, el("p", {}, "calibration.toml と auto.toml をその設定にします。動いている run にも反映します。適用したことも履歴に残ります。"), "適用する"))) return;
  try { const r = await api("/api/restore", { id }); clearEdits(); toast(`適用しました（${r.changes.length} 項目）`); await reload(); }
  catch (e) { toast(`適用できませんでした: ${e.message}`); }
}

async function save() {
  const ids = changedIds();
  if (!ids.length) return;
  const nWarn = ids.filter((id) => warned(byId[id], byId[id].saved, edits[id])).length;
  const t = el("table", {}, el("tr", {}, el("th", {}, "項目"), el("th", { class: "num" }, "保存済み"), el("th", { class: "num" }, "新しい値"), el("th", { class: "num" }, "変化")));
  for (const id of ids) {
    const p = byId[id];
    t.append(el("tr", {}, el("td", {}, p.name), el("td", { class: "num" }, fmt(p.saved, p)), el("td", { class: "num" }, fmt(edits[id], p)),
      el("td", { class: "num" }, pct(rel(p, p.saved, edits[id])), warned(p, p.saved, edits[id]) ? el("span", { class: "warn" }, "⚠") : null)));
  }
  const note = el("input", { type: "text", class: "wide", placeholder: "メモ（任意。履歴に残る）" });
  const ok = await confirmBox(`${ids.length} 項目の変更を確定する`, [t,
    nWarn ? el("p", { class: "warn" }, `保存済みから ${Math.round(WARN * 100)}% 以上動く項目が ${nWarn} 件あります。`) : null,
    el("p", { class: "muted" }, "calibration.toml に書き、スナップショットを残します。run を起動し直すと使われます。"), note], "確定する");
  if (!ok) return;
  try {
    const r = await api("/api/save", { values: Object.fromEntries(ids.map((id) => [id, edits[id]])), note: note.value });
    clearEdits(); toast(`確定しました（${r.changes.length} 項目）`); await reload();
  } catch (e) { toast(`確定できませんでした: ${e.message}`); }
}

// ---------------------------------------------------------------- 測定・見積もり（別プロセス）
async function pollJob(id, boxId, onDone, progress) {
  const box = document.getElementById(boxId);
  box.classList.remove("hidden");
  const logBox = el("pre", {});
  const status = el("div", { class: "small" });
  box.replaceChildren(...[progress ? progress.node : null, status, el("details", { class: "detail" }, el("summary", {}, "出力"), logBox)].filter(Boolean));
  for (;;) {
    let j;
    try { j = await api(`/api/job?id=${id}`); } catch (e) { status.textContent = e.message; return; }
    status.textContent = `${{ running: "実行中", done: "終わりました", failed: "失敗しました" }[j.status]}（${j.elapsed_s} 秒）`;
    logBox.textContent = j.log.split("\n").slice(-60).join("\n");
    if (progress) progress.feed(j);
    if (j.status !== "running") { if (progress) progress.finish(j.status === "done"); onDone(j); return; }
    await new Promise((r) => setTimeout(r, 700));
  }
}

// 見積もりの進み具合: 記録ごとのフレーム数で、記録を流し始めるたびの位置（チェックポイント）を決め、その間をイージングで進める
function evalProgress() {
  const bar = el("div", { class: "pbar-fill" });
  const label = el("span", { class: "muted small" }, "準備中");
  const node = el("div", { class: "pbar-wrap" }, el("div", { class: "pbar" }, bar), label);
  let plan = null, total = 1, done = false;
  const seen = [], startAt = [];
  let shown = 0, target = 0.02;
  const t0 = performance.now();
  function tick() {
    if (!done && plan && seen.length) {
      const k = seen.length;
      const before = plan.slice(0, k - 1).reduce((s, [, f]) => s + f, 0);
      const curF = plan[k - 1][1];
      const startedAt = startAt[k - 1];
      const rate = before > 0 ? before / ((startedAt - t0) / 1000) : 9000; // フレーム/秒（ここまでの実測。初めは目安）
      const frac = 1 - Math.exp(-((performance.now() - startedAt) / 1000) / Math.max(0.3, curF / rate));
      target = Math.min(0.98, 0.03 + 0.95 * (before + curF * 0.95 * frac) / total);
    }
    shown += (target - shown) * 0.12;
    bar.style.width = `${(shown * 100).toFixed(1)}%`;
    if (done && Math.abs(target - shown) <= 0.002) clearInterval(timer);
  }
  // requestAnimationFrame は、ページが裏にあると止まるので、タイマーで進める
  const timer = setInterval(tick, 50);
  return {
    node,
    feed(j) {
      if (!plan && j.plan) { plan = j.plan; total = plan.reduce((s, [, f]) => s + f, 0) || 1; }
      if (!plan) return;
      const names = [...j.log.matchAll(/^\s+(\S+\.jsonl)（\d+ フレーム）/gm)].map((m) => m[1]);
      for (let i = seen.length; i < Math.min(names.length, plan.length); i++) { seen.push(names[i]); startAt.push(performance.now()); }
      if (seen.length) label.textContent = `${seen.length} / ${plan.length} 本目（${seen[seen.length - 1]}）`;
    },
    finish(ok) { done = true; if (ok) target = 1; label.textContent = ok ? "終わりました" : "失敗しました"; },
  };
}

// 測定の指示と進み具合: calibrate は段階に入るたびに指示を出力する。段階の長さは決まっているので、その間は時間で進める
function measureProgress() {
  const now = el("div", { class: "measure-now" }, "準備中…");
  const step = el("span", { class: "muted small" });
  const bar = el("div", { class: "pbar-fill" });
  const node = el("div", { class: "measure-progress" }, now, el("div", { class: "pbar-wrap" }, el("div", { class: "pbar" }, bar), step));
  let plan = null, total = 1, idx = -1, since = 0, shown = 0, target = 0, done = false;
  const timer = setInterval(() => {
    if (!done && plan && idx >= 0) {
      const before = plan.slice(0, idx).reduce((a, [, d]) => a + d, 0);
      target = Math.min(0.99, (before + Math.min(plan[idx][1], (performance.now() - since) / 1000)) / total);
    }
    shown += (target - shown) * 0.15;
    bar.style.width = `${(shown * 100).toFixed(1)}%`;
    if (done && Math.abs(target - shown) < 0.002) clearInterval(timer);
  }, 50);
  return {
    node,
    feed(j) {
      if (!plan && j.plan) { plan = j.plan; total = plan.reduce((a, [, d]) => a + d, 0) || 1; }
      if (!plan) return;
      let i = -1, pos = 0;
      for (let k = 0; k < plan.length; k++) { const at = j.log.indexOf(plan[k][2], pos); if (at < 0) break; i = k; pos = at + plan[k][2].length; }
      if (i !== idx && i >= 0) { idx = i; since = performance.now(); }
      if (idx < 0) return;
      const [label, , msg] = plan[idx];
      const nData = plan.filter(([l]) => l).length, kData = plan.slice(0, idx + 1).filter(([l]) => l).length;
      now.replaceChildren(label ? el("span", { class: "measure-tag" }, `記録 ${kData} / ${nData}`) : el("span", { class: "measure-tag wait" }, "次の準備"), msg);
      step.textContent = `段階 ${idx + 1} / ${plan.length}（カメラのウィンドウにも同じ指示が出ます）`;
    },
    finish(ok) { done = true; if (ok) target = 1; now.textContent = ok ? "測定が終わりました。結果は「測定の候補」と各図に出ます。" : "測定が中断されたか、失敗しました。"; },
  };
}

async function measure(units) {
  try {
    let r = await api("/api/measure", { units, stop_run: false });
    if (r.need_stop) {
      const ok = await confirmBox("run を止めて測定する", el("p", {}, `run が動いています（${r.run.join(", ")}）。測定はカメラを使うので、run を止めます（押下中のボタンとモードは解除されます）。測定の後、run は自動では起動しません。`), "止めて測定する");
      if (!ok) return;
      r = await api("/api/measure", { units, stop_run: true });
    }
    document.querySelectorAll("#measure-buttons button, .unit-btn").forEach((b) => (b.disabled = true));
    document.getElementById("panel-measure").scrollIntoView({ behavior: "smooth" });
    await pollJob(r.job, "measure-job", async () => {
      document.querySelectorAll("#measure-buttons button, .unit-btn").forEach((b) => (b.disabled = false));
      for (const u of units) delete dists[u];
      await reload();
    }, measureProgress());
  } catch (e) { toast(`測定できませんでした: ${e.message}`); }
}

async function evaluate(values, label) {
  try {
    const r = await api("/api/eval", { values, label });
    document.getElementById("eval-result").replaceChildren();
    evalRunning = true; refreshTop();
    await pollJob(r.job, "eval-job", (j) => {
      evalRunning = false; refreshTop();
      if (j.result) renderEval(j.result, label, j.log);
    }, evalProgress());
  } catch (e) { evalRunning = false; refreshTop(); toast(`見積もれませんでした: ${e.message}`); }
}

function diffCell(a, b, higherIsBad) {
  const d = (b || 0) - (a || 0);
  return el("td", { class: `num ${d === 0 ? "" : (d > 0) === higherIsBad ? "up" : "down"}` }, `${a ?? 0} → ${b ?? 0}`);
}

// 見積もりの結果を、節ごとのツリーにまとめる（同じ 1 回の見積もりの結果を、変わる項目・ラベル付きの形・ラベルなしの数で節に振り分ける）
const EVAL_SECTIONS = [
  { title: "カーソルと操作範囲", params: ["cursor"], gest: ["cursor"], metrics: [] },
  { title: "クリック・ピンチ・右クリック", params: ["click"], gest: ["pinch", "right"], metrics: ["click", "right_click", "drag", "drag_suspect"] },
  { title: "ピース（スクロール、戻る・進む）", params: ["peace"], gest: ["peace"], metrics: ["nav", "scroll_px"] },
  { title: "いいね", params: ["thumbsup"], gest: ["thumbsup"], metrics: ["thumbsup"] },
  { title: "ズーム", params: ["zoom"], gest: ["zoom"], metrics: ["zoom"] },
  { title: "使う手・モード・オン・オフ・グー、全体", params: ["gesture", "mode", "power", "fist"], gest: ["fist"], metrics: ["canceled"] },
];
function renderEval(res, label, log) {
  const lt = res.labeled_total, ut = res.unlabeled_total;
  const nodes = [];
  for (const sec of EVAL_SECTIONS) {
    const params = res.params.filter((p) => sec.params.includes(p.key.split(".")[0]));
    const gest = res.labeled.length ? sec.gest.filter((g) => lt.base.segments[g] !== undefined || lt.base.false[g] !== undefined) : [];
    const metrics = res.unlabeled.length ? sec.metrics : [];
    const changedG = gest.filter((g) => (lt.base.reached[g] ?? 0) !== (lt.candidate.reached[g] ?? 0) || (lt.base.false[g] ?? 0) !== (lt.candidate.false[g] ?? 0));
    const changedM = metrics.filter((k) => (ut.base[k] ?? 0) !== (ut.candidate[k] ?? 0));
    if (!params.length && !changedG.length && !changedM.length) continue;
    const kids = [];
    if (params.length) {
      const t = el("table", {}, el("tr", {}, el("th", {}, "変わる項目"), el("th", { class: "num" }, "保存済み → 比べる値"), el("th", { class: "num" }, "変化")));
      for (const p of params) t.append(el("tr", {}, el("td", {}, p.name || p.key), el("td", { class: "num" }, `${fmt(p.base)} → ${fmt(p.candidate)}`),
        el("td", { class: "num" }, p.rel === null ? "" : pct(p.rel), p.warn ? el("span", { class: "warn" }, "⚠") : null)));
      kids.push(t);
    }
    if (gest.length) {
      const t = el("table", {}, el("tr", {}, el("th", {}, "ラベル付きの記録"), el("th", { class: "num" }, "成立した区間"), el("th", { class: "num" }, "誤成立")));
      for (const g of gest) t.append(el("tr", {}, el("td", {}, GJA[g] || g),
        lt.base.segments[g] !== undefined ? diffCell(lt.base.reached[g], lt.candidate.reached[g], false) : el("td", { class: "num muted" }, "—"),
        lt.base.false[g] !== undefined ? diffCell(lt.base.false[g], lt.candidate.false[g], true) : el("td", { class: "num muted" }, "—")));
      kids.push(t);
    }
    if (metrics.length) {
      const t = el("table", {}, el("tr", {}, el("th", {}, `ラベルのない記録（${res.unlabeled.length} 本）`), el("th", { class: "num" }, "保存済み → 比べる値")));
      for (const k of metrics) t.append(el("tr", {}, el("td", {}, MJA[k]), diffCell(ut.base[k], ut.candidate[k], k === "drag_suspect" || k === "canceled")));
      kids.push(t);
    }
    const tags = [params.length ? `変わる項目 ${params.length}` : null, ...changedG.map((g) => `${GJA[g]} 成立 ${lt.base.reached[g] ?? 0}→${lt.candidate.reached[g] ?? 0}・誤 ${lt.base.false[g] ?? 0}→${lt.candidate.false[g] ?? 0}`),
      ...changedM.map((k) => `${MJA[k]} ${ut.base[k] ?? 0}→${ut.candidate[k] ?? 0}`)].filter(Boolean);
    nodes.push(el("details", { class: "tree", open: params.length > 0 }, el("summary", {}, el("strong", {}, sec.title), el("span", { class: "muted small" }, tags.join("、"))), ...kids));
  }
  const extra = [];
  if (res.unlabeled.length) extra.push(el("p", { class: "muted small" }, "ラベルのない記録の数の増減は、良し悪しを表しません（例: 解除が遠いと、続けたクリックが 1 回のドラッグにまとまって数が減る）。詳細の時刻で確かめてください。"));
  if (res.skipped.length) extra.push(el("p", { class: "muted small" }, `使わなかった記録: ${res.skipped.map((x) => x.path.split("/").pop()).join(", ")}`));
  const root = el("details", { class: "tree tree-root", open: true }, el("summary", {}, el("strong", {}, `${label}の見積もり`),
    el("span", { class: "muted small" }, `変わる項目 ${res.params.length}、警告 ${res.params.filter((p) => p.warn).length}`)),
    ...(nodes.length ? nodes : [el("p", { class: "muted" }, "差はありませんでした。")]), ...extra,
    el("details", { class: "tree" }, el("summary", {}, "詳細（記録ごと、変わったイベントの時刻）"), el("pre", { class: "out" }, log)));
  document.getElementById("eval-result").replaceChildren(root);
}

// ---------------------------------------------------------------- ライブ（run の今の値。SPEC 13.47）
const MODE_JA = { idle: "なし", cursor: "カーソル", pinch: "ピンチ（左）", drag: "ドラッグ", right: "右クリック待ち", peace: "ピース",
  thumbsup: "いいね", zoom: "ズーム", fist: "グー" };
const BONES = [[0, 1], [1, 2], [2, 3], [3, 4], [0, 5], [5, 6], [6, 7], [7, 8], [5, 9], [9, 10], [10, 11], [11, 12], [9, 13], [13, 14],
  [14, 15], [15, 16], [13, 17], [17, 18], [18, 19], [19, 20], [0, 17]];
// メーター: [名前, 今の値の取り出し, 範囲, 閾値（項目の id か、run の設定の名前）, 小さいほど成立か]
const METERS = [
  ["親指-人差し指", (s) => s.ti, [0, 0.8], ["click.pinch_on", "click.pinch_off", "click.drag_off"]],
  ["親指-中指", (s) => s.tm, [0, 0.8], ["click.right_on", "click.right_off", "click.right_arm"]],
  ["親指-手の平の中心", (s) => s.thumb_far, [0, 3], ["thumbsup.thumbsup_thumb_far_min", "thumbsup.thumbsup_stay_thumb_far_min"]],
  ["親指の畳み", (s) => s.thumb_fold, [-1, 1], ["peace.thumb_fold_on", "peace.thumb_fold_off"]],
  ["人差し指の真っすぐさ", (s) => s.straight?.[0], [0.3, 1], ["cfg:index_up_enter", "cfg:index_up_exit", "cfg:peace_exit_straight"]],
  ["中指の真っすぐさ", (s) => s.straight?.[1], [0.3, 1], ["cfg:index_up_enter", "cfg:index_up_exit", "cfg:peace_exit_straight"]],
  ["薬指の真っすぐさ", (s) => s.straight?.[2], [0.3, 1], ["cfg:ring_down_max"]],
  ["手の甲の向き", (s) => s.palm_facing, [-1, 1], ["cfg:zoom_back_min"]],
];
const live = { es: null, snap: null, hooks: [], sendTimer: null, available: false };

function thrValue(key, snap) {
  if (key.startsWith("cfg:")) return snap.cfg?.[key.slice(4)];
  return byId[key] ? cur(key) : undefined;
}

function renderLive(snap) {
  const body = document.getElementById("live-body");
  body.classList.remove("hidden");
  const m = document.getElementById("live-mode");
  m.replaceChildren(...[el("span", { class: "live-mode-name" }, snap.hand ? MODE_JA[snap.mode] || snap.mode : "手が見えません"),
    el("span", { class: `chip ${snap.active ? "on" : ""}` }, snap.active ? "有効化中" : "有効化していない（右 Command でオン）"),
    snap.pending ? el("span", { class: "muted small" }, "差し替え待ち（操作の途中）") : null].filter(Boolean));
  // 手の骨格（利用者から見た向き）と操作範囲
  const [iw, ih] = snap.image_size;
  const W = 320, H = (W * ih) / iw;
  const svg = newSvg(W, H);
  svgEl(svg, "rect", { class: "frame", x: 0, y: 0, width: W, height: H });
  if (snap.roi) {
    const [cx, cy, w] = snap.roi;
    const [sw, sh] = S.screen_size;
    const h = (w * iw * (sh / sw)) / ih;
    svgEl(svg, "rect", { class: "roi-edit", x: (1 - (cx + w / 2)) * W, y: (cy - h / 2) * H, width: w * W, height: h * H });
  }
  if (snap.hand && snap.landmarks) {
    const P = snap.landmarks.map(([x, y]) => [(1 - x) * W, y * H]);
    for (const [a, b] of BONES) svgEl(svg, "line", { class: "bone", x1: P[a][0], y1: P[a][1], x2: P[b][0], y2: P[b][1] });
    P.forEach(([x, y], i) => svgEl(svg, "circle", { class: [4, 8, 12].includes(i) ? "joint tip" : "joint", cx: x, cy: y, r: [4, 8, 12].includes(i) ? 3.5 : 2.2 }));
  }
  document.getElementById("live-hand").replaceChildren(svg);
  // メーター
  const box = document.getElementById("live-meters");
  box.replaceChildren(...METERS.map(([name, get, [lo, hi], thr]) => {
    const v = snap.hand ? get(snap) : null;
    const MW = 260, MH = 16, X = (x) => ((Math.min(hi, Math.max(lo, x)) - lo) / (hi - lo)) * MW;
    const sv = newSvg(MW, MH);
    sv.classList.add("meter");
    sv.setAttribute("preserveAspectRatio", "none");
    svgEl(sv, "rect", { class: "bar-bg", x: 0, y: 2, width: MW, height: MH - 4, rx: 3 });
    if (num(v)) svgEl(sv, "rect", { class: "meter-val", x: 0, y: 2, width: X(v), height: MH - 4, rx: 3 });
    thr.forEach((k, i) => {
      const t = thrValue(k, snap);
      if (!num(t)) return;
      svgEl(sv, "line", { class: `thr k-${i}`, x1: X(t), x2: X(t), y1: 0, y2: MH });
    });
    return el("div", { class: "meter-row" }, el("span", { class: "meter-name" }, name), sv, el("span", { class: "meter-num" }, num(v) ? v.toFixed(2) : "—"));
  }));
  const out = document.getElementById("live-output");
  if (document.activeElement !== out) out.checked = !!snap.output;
  setOutputIcon(!!snap.output);
  for (const h of live.hooks) h(snap);
}

function connectLive() {
  if (live.es) return;
  const es = new EventSource(`/api/live/stream?t=${encodeURIComponent(TOKEN)}`);
  live.es = es;
  const chip = document.getElementById("live-chip");
  chip.textContent = "接続中…";
  es.onopen = () => { chip.textContent = "設定モード（run とつながっています）"; chip.classList.add("on"); document.body.classList.add("live-on"); sendLiveConfig(); };
  es.onmessage = (e) => { live.snap = JSON.parse(e.data); renderLive(live.snap); };
  es.onerror = () => { // run が終わった・つながらない: 閉じて、次の確認でつなぎ直す
    es.close(); live.es = null; live.snap = null;
    chip.textContent = "run に接続していません"; chip.classList.remove("on"); document.body.classList.remove("live-on"); setOutputIcon(false);
    document.getElementById("live-body").classList.add("hidden");
    for (const h of live.hooks) h(null);
  };
}

// 閉じているときのつまみの手のアイコン: 操作に反映しているか
function setOutputIcon(on) {
  document.body.classList.toggle("output-on", on);
  document.querySelector("#live-handle .out-icon").title = on ? "操作に反映する（オン）" : "操作に反映する（オフ）";
}

// 変更中の値を run に送る（少し待ってまとめて。保存はしない）
function sendLiveConfig() {
  if (!live.es) return;
  clearTimeout(live.sendTimer);
  live.sendTimer = setTimeout(async () => {
    const values = Object.fromEntries(changedIds().filter((id) => id !== "gesture.hand").map((id) => [id, edits[id]]));
    try { await api("/api/live/config", { values }); } catch (e) { toast(`run に送れませんでした: ${e.message}`); }
  }, 150);
}

// ---------------------------------------------------------------- 起動
async function loadDists() {
  await Promise.all(["range", "pinch", "right", "peace", "thumbsup", "zoom"].filter((u) => !dists[u]).map(async (u) => {
    try { dists[u] = await api(`/api/dist?unit=${u}`); } catch (e) { dists[u] = { available: false, why: e.message }; }
  }));
}

async function reload() {
  S = await api("/api/state");
  for (const k of Object.keys(byId)) delete byId[k];
  for (const p of S.params) byId[p.id] = p;
  for (const id of Object.keys(edits)) if (!byId[id]) delete edits[id];
  await loadDists();
  renderSections();
  renderCandidates();
  renderHistory();
  refreshTop();
}

function setup() {
  const mb = document.getElementById("measure-buttons");
  for (const [u, name] of UNITS) mb.append(el("button", { class: "unit-cell", onclick: () => measure([u]) }, name));
  mb.append(el("button", { class: "range-btn span4", onclick: () => measure(["range", "pinch", "right", "peace"]) }, "U1〜U4：コア操作"),
    el("button", { class: "range-btn span6", onclick: () => measure(UNITS.map((x) => x[0])) }, "U1〜U6：全操作（約 3 分）"));
  document.getElementById("btn-save").onclick = save;
  document.getElementById("btn-discard").onclick = () => { clearEdits(); refreshAll(); };
  document.getElementById("btn-undo").onclick = async () => {
    if (!(await confirmBox("前の保存に戻す", el("p", {}, `最新の保存（${S.history[0]?.route}）の 1 つ前の状態に戻します。`), "戻す"))) return;
    try { const r = await api("/api/undo", {}); clearEdits(); toast(`戻しました（${r.changes.length} 項目）`); await reload(); }
    catch (e) { toast(`戻せませんでした: ${e.message}`); }
  };
  document.getElementById("btn-name").onclick = async () => {
    const name = document.getElementById("name-input").value.trim();
    if (!name) return toast("名前を入れてください");
    try { await api("/api/name", { name }); toast(`「${name}」を残しました（保存済みの状態）`); await reload(); }
    catch (e) { toast(`残せませんでした: ${e.message}`); }
  };
  document.getElementById("btn-eval-measured").onclick = () => {
    const rows = measuredDiffs();
    if (!rows.length) return toast("保存済みと違う測定の値がありません");
    evaluate(Object.fromEntries(rows.map((p) => [p.id, p.measured.value])), "測定の候補");
  };
  document.getElementById("btn-eval-edit").onclick = () => evaluate(Object.fromEntries(changedIds().map((id) => [id, edits[id]])), "変更中の値");
  window.addEventListener("beforeunload", (e) => { if (changedIds().length) { e.preventDefault(); e.returnValue = ""; } });
  document.getElementById("btn-quit").onclick = quit;
  runTimer = setInterval(async () => {
    try { const r = await api("/api/run"); S.run = r.run; live.available = r.live; refreshTop(); if (r.live) connectLive(); }
    catch (e) { /* サーバーが止まった */ }
  }, 3000);
  // ライブの引き出し（右）。開いているかは、このブラウザに覚える（使えなければ覚えない）
  const setDrawer = (open) => {
    document.body.classList.toggle("drawer-open", open);
    document.getElementById("live-handle").setAttribute("aria-expanded", String(open));
    try { localStorage.setItem("airctl-live-open", open ? "1" : "0"); } catch (e) { /* 覚えない */ }
  };
  let wasOpen = true;
  try { wasOpen = localStorage.getItem("airctl-live-open") !== "0"; } catch (e) { /* 既定は開く */ }
  setDrawer(wasOpen);
  document.getElementById("live-handle").onclick = () => setDrawer(true);
  document.getElementById("live-close").onclick = () => setDrawer(false);
  document.getElementById("live-output").onchange = async (e) => {
    try { await api("/api/live/mode", { output: e.target.checked }); setOutputIcon(e.target.checked); }
    catch (err) { e.target.checked = false; toast(`切り替えられませんでした: ${err.message}`); }
  };
  reload().then(async () => { try { if ((await api("/api/run")).live) connectLive(); } catch (e) { /* なし */ } })
    .catch((e) => toast(`読み込めませんでした: ${e.message}`));
}
setup();
