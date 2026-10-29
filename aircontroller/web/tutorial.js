// チュートリアルとジェスチャー別のテスト（TASKS-20261027-package 第6節、SPEC 13.60）。
// 一覧: スキルツリー（系統ごとに円をつなぐ）。円にカーソルを合わせると説明のカード、押すと練習の画面（左に画像・説明・コツ、右に練習）。
// 画像は手のイラスト 2〜4 枚の切り替え（ライト・ダークの 2 組。SPEC 13.63）（tutorial/manifest.json。画像が無ければ枠と一言の仮の表示）。
// 練習の間は run に練習モードでつなぐ（キーの出力だけを止め、マウスは出す）。発火したイベントと今の値を受け取り、キーの操作は画面の中で再現する。
// 合格はデータの場所の tutorial-progress.json に残す（SPEC 13.65）。合格済みの円にチェックの印、10 個目の合格で完走の画面を 1 回出す。
"use strict";
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);
const MODE_JA = { idle: "待機", cursor: "カーソル", pinch: "ピンチ", drag: "ドラッグ", right: "右クリック待ち", peace: "ピース",
  thumbsup: "いいね", zoom: "ズーム", fist: "グー" };

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false && c !== "") e.append(c instanceof Node ? c : String(c));
  return e;
}

let M = null, cur = 0, animTimer = null;

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

// ---------------------------------------------------------------- 合格の記録（SPEC 13.65）
let P = { passed: {}, completed_at: null };
const when = (iso) => new Date(iso).toLocaleString("ja-JP", { year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" });

function showProgress() {
  for (const n of nodes) n.classList.toggle("passed", !!P.passed[n._step.id]);
  $("done-mark").classList.toggle("hidden", !P.completed_at);
}

function showDone() {
  $("done-when").textContent = P.completed_at ? when(P.completed_at) : "";
  $("done").classList.remove("hidden");
  $("btn-done-close").focus();
}

// 記録に失敗しても、練習の合格はそのまま（画面は止めない）
function recordPass(id) {
  api("/api/tutorial/pass", { id }).then((out) => {
    P = out;
    showProgress();
    if (out.completed_now) showDone();
  }).catch(() => {});
}

// ---------------------------------------------------------------- 画像の切り替え（画像が無ければ、枠と一言とファイル名）
// 画像は、ライトとダークのテーマの 2 組（背景は透明。ダークは <id>-<番号>-dark）。読み込めたら onload(picture) を呼ぶ
function themedImage(id, n, attrs, onload) {
  const q = `?t=${encodeURIComponent(TOKEN)}`, base = `/${M.image_dir}/${id}-${n}`;
  const pic = el("picture", {}, el("source", { media: "(prefers-color-scheme: dark)", srcset: `${base}-dark.${M.ext}${q}` }));
  const img = el("img", { ...attrs, onload: () => onload(pic) });
  pic.append(img);
  img.src = `${base}.${M.ext}${q}`;
}

function frames(s, w, h) {
  const fs = [...Array(s.frames)].map((_, k) => {
    const f = el("div", { class: "tut-frame", style: `width:${w}px;height:${h}px` },
      el("div", { class: "tut-ph" }, el("strong", {}, s.frame_text?.[k] || `${k + 1} 枚目`),
        el("span", { class: "muted small" }, `${s.id}-${k + 1}.${M.ext}`)));
    themedImage(s.id, k + 1, { alt: s.frame_text?.[k] || "", width: w, height: h }, (pic) => f.replaceChildren(pic));
    return f;
  });
  const dots = el("div", { class: "tut-dots" }, fs.map(() => el("span")));
  let k = 0, timer = null;
  const show = (n) => { fs.forEach((f, j) => f.classList.toggle("on", j === n)); [...dots.children].forEach((d, j) => d.classList.toggle("on", j === n)); };
  show(0);
  return {
    node: el("div", { class: "tut-frames" }, ...fs, dots),
    play() { clearInterval(timer); k = 0; show(0); timer = setInterval(() => { k = (k + 1) % fs.length; show(k); }, M.interval_ms); },
    stop() { clearInterval(timer); show(0); },
  };
}

// ---------------------------------------------------------------- 一覧（スキルツリー）
// 円（操作）を、オン・オフを中心に放射状に、つながり（manifest の parent）で線でつなぐ。系統（group）は色だけで表す（名前は出さない）。
// 大きさと位置は、枠の幅から毎回計算する（ウィンドウを小さくしても崩れない）。円にカーソルを合わせると、説明のカードが出る。
const GROUP_COLOR = ["--k5", "--k0", "--k3", "--k4", "--k2"];
let nodes = [];

function card(s, i) {
  const fr = frames(s, ...M.tile_size);
  const c = el("div", { class: "tree-card", role: "button", tabindex: -1, onclick: (e) => { e.stopPropagation(); openStep(i); } },
    el("div", { class: "tree-card-body" },
      el("div", { class: "tile-img" }, fr.node),
      el("div", { class: "tree-card-text" },
        el("strong", { class: "tree-card-title" }, s.title),
        el("ul", { class: "tut-lines" }, s.lines.map((l) => el("li", {}, l))),
        el("div", { class: "tile-tips" }, el("span", { class: "muted small" }, "コツ"), el("ul", { class: "tut-lines muted" }, s.tips.map((l) => el("li", {}, l)))))),
    el("div", { class: "tree-card-go" }, "練習する ›"));
  c._frames = fr;
  return c;
}

function buildTree() {
  const tree = $("tree");
  nodes = M.steps.map((s, i) => {
    const g = Math.max(0, M.groups.indexOf(s.group));
    const ring = el("div", { class: "tree-ring" }, el("span", { class: "tree-dot" }));  // 画像が無い間は、系統の色の点
    themedImage(s.id, 1, { alt: "" }, (pic) => ring.replaceChildren(pic)); // 画像があれば円の中に出す
    const cd = card(s, i);
    const node = el("div", { class: "tree-node", tabindex: 0, role: "button", "aria-label": s.title, style: `--c: var(${GROUP_COLOR[g]})`,
      onclick: () => openStep(i), onkeydown: (e) => { if (e.key === "Enter") openStep(i); },
      onmouseenter: () => { placeCard(node, cd); cd._frames.play(); }, onmouseleave: () => cd._frames.stop(),
      onfocus: () => placeCard(node, cd) },
    ring, el("span", { class: "tree-check", "aria-hidden": "true" }, "✓"), el("span", { class: "tree-label" }, s.title), cd);
    node._step = s;
    tree.append(node);
    return node;
  });
  new ResizeObserver(layoutTree).observe(tree);
  layoutTree();
}

// 放射状の配置: オン・オフ（根）を中心に、つながりの深さで外へ広げる。角度は、葉（先端の操作）の数に比例して配る。
// 返り値は id → [角度（ラジアン）, 深さ]
function radial() {
  const kids = (id) => M.steps.filter((s) => s.parent === id);
  const leaves = (id) => { const k = kids(id); return k.length ? k.reduce((a, c) => a + leaves(c.id), 0) : 1; };
  const root = M.steps.find((s) => !s.parent);
  const out = { [root.id]: [0, 0] };
  const total = leaves(root.id);
  const place = (id, a0, a1, depth) => {
    let a = a0;
    for (const c of kids(id)) {
      const span = ((a1 - a0) * leaves(c.id)) / leaves(id);
      out[c.id] = [a + span / 2, depth];
      place(c.id, a, a + span, depth + 1);
      a += span;
    }
  };
  // 最初の枝（カーソル移動。いちばん長い）が右を向くように。横長のウィンドウに合う
  const first = kids(root.id)[0];
  const start = first ? -Math.PI * leaves(first.id) / total : 0;
  place(root.id, start, start + 2 * Math.PI, 1);
  return out;
}

function layoutTree() {
  const tree = $("tree"), W = tree.clientWidth;
  if (!W || !nodes.length) return;
  const pol = radial();
  const ids = nodes.map((n) => n._step.id);
  const turn = W < 760 ? Math.PI / 2 : 0; // 狭いときは 4 分の 1 回して、いちばん長い枝を下に向ける（横幅が少なくて済む）
  const u = ids.map((id) => [Math.cos(pol[id][0] + turn) * pol[id][1], Math.sin(pol[id][0] + turn) * pol[id][1]]); // 単位の位置
  const minX = Math.min(...u.map((p) => p[0])), maxX = Math.max(...u.map((p) => p[0]));
  const minY = Math.min(...u.map((p) => p[1])), maxY = Math.max(...u.map((p) => p[1]));
  // 横の広さに合わせて横の倍率と円の大きさを決め、縦の倍率は円が重ならないところまで広げる（一覧はスクロールしてよい）
  let size = 120, pad = 0, sx = 0;
  for (let k = 0; k < 2; k++) {
    pad = size * 0.6 + 34; // 円の半分と操作名の分
    sx = (W - pad * 2) / Math.max(maxX - minX, 1);
    size = Math.round(Math.max(64, Math.min(150, sx / 1.5)));
  }
  pad = size * 0.6 + 34;
  let sy = turn ? sx : sx * 0.55, xy = [];
  const place = () => { xy = u.map(([x, y]) => [pad + (x - minX) * sx, pad + (y - minY) * sy]); };
  // 重なり: 円どうしが近い、または縦に並んだときに下の操作名（2 行まで）とぶつかる
  const tooClose = () => xy.some((p, i) => xy.some((q, j) => j > i && (Math.hypot(p[0] - q[0], p[1] - q[1]) < size * 1.45
    || (Math.abs(p[0] - q[0]) < size * 1.3 && Math.abs(p[1] - q[1]) < size + 52))));
  for (let k = 0; k < 40; k++) { // 重なるなら縦に広げ、それでも足りなければ円を小さくする
    place();
    if (!tooClose()) break;
    if (k % 8 === 7 && size > 56) { size = Math.round(size * 0.9); pad = size * 0.6 + 34; sx = (W - pad * 2) / Math.max(maxX - minX, 1); } else sy *= 1.08;
  }
  const H = pad * 2 + (maxY - minY) * sy;
  tree.style.height = `${H}px`;
  tree.style.setProperty("--size", `${size}px`);
  nodes.forEach((n, i) => { n.style.left = `${xy[i][0]}px`; n.style.top = `${xy[i][1]}px`; });
  // 線: 親の円の縁から子の円の縁へ
  const svg = $("tree-lines");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.replaceChildren(...nodes.flatMap((n, i) => {
    const pi = M.steps.findIndex((s) => s.id === n._step.parent);
    if (pi < 0) return [];
    const [x1, y1] = xy[pi], [x2, y2] = xy[i];
    const len = Math.hypot(x2 - x1, y2 - y1) || 1, ux = (x2 - x1) / len, uy = (y2 - y1) / len, r = size / 2 + 6;
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", `M${x1 + ux * r},${y1 + uy * r} L${x2 - ux * r},${y2 - uy * r}`);
    p.style.stroke = getComputedStyle(n).getPropertyValue("--c"); // style 属性は CSP で止まるので、プロパティで設定する
    return [p];
  }));
  const open = nodes.find((n) => n.matches(":hover, :focus-visible")); // 開いているカードは、新しい位置に置き直す
  if (open) placeCard(open, open.querySelector(".tree-card"));
}

// 説明のカードを、円の右（入らなければ左）に、枠の中に収めて出す。カードの位置の基準は円の箱の左上（円は中心で置いている）
function placeCard(node, cd) {
  const tree = $("tree"), W = tree.clientWidth, H = tree.clientHeight;
  const size = node.offsetWidth, x0 = node.offsetLeft - size / 2, y0 = node.offsetTop - size / 2; // 円の箱の左上（枠の中の位置）
  const cw = Math.min(560, W - 16);
  cd.style.width = `${cw}px`;
  let left = size + 12; // 円の右
  if (x0 + left + cw > W - 8) left = -(12 + cw); // 入らなければ左
  left = Math.max(8 - x0, Math.min(left, W - 8 - cw - x0)); // どちらも入らない（狭い）ときは、枠の中に収める
  cd.style.left = `${left}px`;
  const ch = cd.offsetHeight || 280;
  let top = size / 2 - ch / 2;
  top = Math.max(-y0, Math.min(top, H - ch - y0));
  cd.style.top = `${top}px`;
}

function showList() {
  stopTest();
  clearInterval(animTimer);
  $("view-practice").classList.add("hidden");
  $("view-list").classList.remove("hidden");
}

// ---------------------------------------------------------------- 練習の画面（左: 画像・説明・コツ、右: 練習）
let anim = null;
function openStep(i) {
  cur = (i + M.steps.length) % M.steps.length;
  const s = M.steps[cur];
  stopTest();
  $("view-list").classList.add("hidden");
  $("view-practice").classList.remove("hidden");
  $("step-title").textContent = s.title;
  $("step-lines").replaceChildren(...s.lines.map((l) => el("li", {}, l)));
  $("step-tips").replaceChildren(...s.tips.map((l) => el("li", {}, l)));
  anim?.stop();
  anim = frames(s, ...M.size);
  $("anim").replaceChildren(anim.node);
  anim.play();
  const test = TESTS[s.id];
  $("test-title").textContent = `練習: ${s.title}`;
  $("btn-test").disabled = !test;
  $("btn-test").textContent = "練習を始める";
  $("test-task").textContent = test ? test.task : "この操作の練習はありません";
  $("test-area").replaceChildren();
  setHint("");
  setState(test ? "—" : "準備中");
}

// ---------------------------------------------------------------- 練習（run に練習モードでつなぐ）
let es = null, snap = null, running = null;

function setState(t, on = false) { $("test-state").textContent = t; $("test-state").classList.toggle("on", on); }
function setHint(t) { $("test-hint").textContent = t; $("test-hint").classList.toggle("hidden", !t); }

function connect() {
  es = new EventSource(`/api/live/stream?t=${encodeURIComponent(TOKEN)}&practice=1`);
  es.onmessage = (e) => {
    snap = JSON.parse(e.data);
    if (!running) return;
    for (const ev of snap.events || []) running.onEvent?.(ev);
    running.onSnap?.(snap);
    setHint(running.done ? "" : (running.ownHint ? "" : commonHint(snap)) || running.hint?.(snap) || "");
  };
  es.onerror = () => { if (running && !running.done) setHint("run とつながっていません（ホームで「開始」してください）"); };
}

function stopTest() {
  if (es) { es.close(); es = null; }
  running?.cleanup?.();
  running = null;
}

function startTest() {
  const s = M.steps[cur], test = TESTS[s.id];
  stopTest();
  running = test.make($("test-area"), (msg) => { // 合格
    running.done = true;
    setState("合格", true);
    setHint("");
    recordPass(s.id);
    $("btn-test").textContent = "もう一度";
    $("test-task").textContent = msg;
    if (es) { es.close(); es = null; } // 練習モードを抜ける（キーの出力を戻す）
  });
  running.ownHint = !!test.ownHint;
  setState("練習中");
  $("btn-test").textContent = "やり直す";
  $("test-task").textContent = test.task;
  connect();
}

// 手が見えない・オフなど、どのジェスチャーにも共通のヒント（文言は句点を避け、秒数は書かない: 作者の指示）
function commonHint(s) {
  if (!s.active) return "オフです（右 Command を押して離すか、手のひらを短時間向けるとオン）";
  if (!s.hand) return "手が見えていません（手をカメラの正面に出してください）";
  return "";
}
const pct = (v) => (v == null ? "—" : v.toFixed(2));
const modeName = (m) => MODE_JA[m] || m;

// 練習の的（枠の中のランダムな位置）
function spot(area, size) {
  const r = area.getBoundingClientRect();
  return { x: 20 + Math.random() * Math.max(0, r.width - size - 40), y: 20 + Math.random() * Math.max(0, r.height - size - 40) };
}
function progress(text) { return el("div", { class: "test-progress" }, text); }

// 遊びの一言（SPEC 13.64）: 枠の中に小さく出して、少しで消す（stay なら残す）。合格の判定には関わらない。
// 位置は枠の中の座標（省くと上の中央）
function quip(area, text, at, stay = false) {
  const q = el("div", { class: `quip${stay ? " stay" : ""}${at ? " at" : ""}` }, text);
  area.append(q);
  if (at) { // 指した所の少し上。枠からはみ出さないように収める
    const w = q.offsetWidth, h = q.offsetHeight, W = area.clientWidth, H = area.clientHeight;
    q.style.left = `${Math.max(6, Math.min(at.x - w / 2, W - w - 6))}px`;
    q.style.top = `${Math.max(6, Math.min(at.y - h - 12, H - h - 6))}px`;
  }
  if (!stay) q.addEventListener("animationend", () => q.remove());
  return q;
}
// 画面の座標 → 枠の中の座標
function inArea(area, x, y) { const r = area.getBoundingClientRect(); return { x: x - r.left, y: y - r.top }; }

// ピース（スクロール・戻る・進む）の形のヒント
function peaceHint(s) {
  const [, md, rg] = s.straight || [];
  if (md != null && md < s.cfg.index_up_enter) return `中指をもっと立ててください（真っすぐさ ${pct(md)}、${s.cfg.index_up_enter} を超えると立っている）`;
  if (s.thumb_fold != null && s.thumb_fold < s.cfg.thumb_fold_on) return `親指をもっと畳んでください（畳み ${pct(s.thumb_fold)}、${s.cfg.thumb_fold_on} を超えると畳んでいる）`;
  if (rg != null && rg >= s.cfg.ring_down_max) return `薬指を曲げてください（真っすぐさ ${pct(rg)}、${s.cfg.ring_down_max} 未満で曲げている）`;
  return "人差し指と中指を立て、親指と薬指を曲げて（ピース）、少し保ってください";
}
// いいね（デスクトップ移動・Mission Control）の形のヒント
function thumbsHint(s) {
  if (s.straight?.[0] != null && s.straight[0] >= s.cfg.thumbsup_index_max) return `人差し指を握ってください（真っすぐさ ${pct(s.straight[0])}、${s.cfg.thumbsup_index_max} 未満で握っている）`;
  if (s.thumb_up != null && s.thumb_up <= s.cfg.thumbsup_up_min) return `親指をもっと上に向けてください（上向き ${pct(s.thumb_up)}、${s.cfg.thumbsup_up_min} を超えると上向き）`;
  return "親指を立てて、ほかの指を握って（いいね）、少し保ってください";
}

// 画面の中の仮のデスクトップ（デスクトップ移動・Mission Control）
const DESKTOP_NAMES = ["作業", "休憩", "遊び"];
function desktops(area, n = 3, at = 1) {
  const strip = el("div", { class: "fake-desktops" }, [...Array(n)].map((_, i) =>
    el("div", { class: "fake-desktop" }, el("span", {}, DESKTOP_NAMES[i] || `デスクトップ ${i + 1}`), el("div", { class: "fake-win" }), el("div", { class: "fake-win b" }))));
  const view = el("div", { class: "fake-screen" }, strip);
  area.append(view);
  const d = { at, n, view, strip, set(i) { d.at = Math.max(0, Math.min(n - 1, i)); strip.style.transform = `translateX(${-100 * d.at}%)`; } };
  d.set(at);
  return d;
}

const TESTS = {
  cursor: {
    task: "カーソルを、出てくる丸に合わせて少し止めてください（5 回）",
    make(area, pass) {
      const N = 5, SIZE = 90, HOLD = 500;
      let n = 0, since = null;
      const t0 = performance.now();
      const dot = el("div", { class: "test-target", style: `width:${SIZE}px;height:${SIZE}px` });
      const prog = progress(`あと ${N} 個`);
      area.replaceChildren(dot, prog);
      const place = () => { const p = spot(area, SIZE); dot.style.left = `${p.x}px`; dot.style.top = `${p.y}px`; since = null; };
      place();
      const timer = setInterval(() => {
        if (!dot.matches(":hover")) { since = null; dot.classList.remove("hot"); return; }
        since ??= performance.now();
        dot.classList.add("hot");
        if (performance.now() - since < HOLD) return;
        n += 1;
        prog.textContent = n >= N ? "全部止められた" : `あと ${N - n} 個`;
        dot.classList.remove("hot");
        if (n >= N) { clearInterval(timer); dot.remove(); quip(area, "全部止められた"); pass(`合格: 5 回合わせました（${((performance.now() - t0) / 1000).toFixed(1)} 秒）`); return; }
        place();
      }, 50);
      return {
        cleanup: () => clearInterval(timer),
        hint(s) {
          if (s.mode === "cursor") return "";
          if (s.mode === "fist") return "グーになっています（人差し指を立ててください）";
          const st = s.straight?.[0];
          if (st != null && st < s.cfg.index_up_enter) return `人差し指をもっとまっすぐ立ててください（真っすぐさ ${pct(st)}、${s.cfg.index_up_enter} を超えると立っている）`;
          return `今は「${modeName(s.mode)}」です（人差し指だけを立ててください）`;
        },
      };
    },
  },
  click: {
    task: "出てくるボタンを、つまんで離してクリックしてください（5 回）",
    make(area, pass) {
      const N = 5, SIZE = 70;
      let n = 0, miss = 0, streak = 0, lastCancel = null;
      const btn = el("button", { class: "test-target btn", style: `width:${SIZE}px;height:${SIZE}px` }, "押す");
      const prog = progress(`0 / ${N}`);
      area.replaceChildren(btn, prog);
      const place = () => { const p = spot(area, SIZE); btn.style.left = `${p.x}px`; btn.style.top = `${p.y}px`; };
      place();
      btn.onclick = (e) => {
        e.stopPropagation();
        n += 1;
        streak += 1;
        prog.textContent = `${n} / ${N}（外れ ${miss}）`;
        if (streak >= 2) quip(area, `連続${streak}回`, inArea(area, e.clientX, e.clientY));
        if (n >= N) { pass(`合格: 5 回クリックしました（外れ ${miss} 回）`); return; }
        place();
      };
      const onArea = (e) => {
        miss += 1;
        streak = 0;
        prog.textContent = `${n} / ${N}（外れ ${miss}）`;
        quip(area, "惜しい", inArea(area, e.clientX, e.clientY));
      };
      area.addEventListener("click", onArea);
      return {
        cleanup: () => area.removeEventListener("click", onArea),
        onEvent(ev) { if (ev.kind === "pinch_cancel") lastCancel = ev; else if (ev.kind === "click") lastCancel = null; },
        hint(s) {
          if (lastCancel?.reason === "posture") return "つまんでいる間に指の形が崩れて、取り消されました（人差し指を立てたまま、親指を寄せてください）";
          if (lastCancel && lastCancel.button === "left") return "つまむ時間が長すぎたか、短すぎました（短くつまんで、すぐ離してください）";
          if (s.mode === "drag") return "つまんだまま待つと、ドラッグになります（短くつまんで離してください）";
          if (s.mode !== "cursor" && s.mode !== "pinch") return `今は「${modeName(s.mode)}」です（人差し指を立てて、カーソルを動かせる形にしてください）`;
          if (s.ti != null && s.mode === "cursor") return `つまむ距離: 今 ${pct(s.ti)}（${s.cfg.pinch_on} 未満でつまんだと判定）`;
          return "";
        },
      };
    },
  },
  drag: {
    task: "カードをつまんだまま少し待ってドラッグにし、点線の箱まで運んで離してください（2 回）",
    make(area, pass) {
      const N = 2;
      let n = 0, drag = null;
      const card = el("div", { class: "drag-card" }, "カード");
      const box = el("div", { class: "drop-box" }, "ここに置く");
      const prog = progress(`0 / ${N}`);
      area.replaceChildren(box, card, prog);
      let settle = null;
      const place = () => {
        card.classList.remove("settle");
        const r = area.getBoundingClientRect();
        const left = n % 2 === 0;
        card.style.left = `${left ? 40 : r.width - 200}px`; card.style.top = `${r.height / 2 - 40}px`;
        box.style.left = `${left ? r.width - 260 : 40}px`; box.style.top = `${r.height / 2 - 80}px`;
      };
      place();
      card.addEventListener("mousedown", (e) => { clearTimeout(settle); card.classList.remove("settle"); drag = { dx: e.clientX - card.offsetLeft, dy: e.clientY - card.offsetTop }; card.classList.add("hot"); e.preventDefault(); });
      const move = (e) => { if (!drag) return; card.style.left = `${e.clientX - drag.dx}px`; card.style.top = `${e.clientY - drag.dy}px`; };
      const up = () => {
        if (!drag) return;
        drag = null;
        card.classList.remove("hot");
        const a = card.getBoundingClientRect(), b = box.getBoundingClientRect();
        const cx = a.left + a.width / 2, cy = a.top + a.height / 2;
        if (cx > b.left && cx < b.right && cy > b.top && cy < b.bottom) {
          // 箱の中央へ落とし込む（合格の判定は先に済ませ、動きは後から付いてくる）
          card.classList.add("settle");
          card.style.left = `${box.offsetLeft + (box.offsetWidth - card.offsetWidth) / 2}px`;
          card.style.top = `${box.offsetTop + (box.offsetHeight - card.offsetHeight) / 2}px`;
          n += 1;
          prog.textContent = `${n} / ${N}`;
          if (n >= N) { pass("合格: 2 回運べました"); return; }
          settle = setTimeout(place, 900);
        }
      };
      area.addEventListener("mousemove", move);
      addEventListener("mouseup", up);
      return {
        cleanup: () => { clearTimeout(settle); area.removeEventListener("mousemove", move); removeEventListener("mouseup", up); },
        hint(s) {
          if (s.mode === "drag") return "ドラッグ中です（つまんだまま箱まで動かし、指をはっきり開いて離してください）";
          if (s.mode === "pinch") return "つまんだまま、手を動かさずに少し待つとドラッグになります";
          if (s.mode !== "cursor") return `今は「${modeName(s.mode)}」です（人差し指を立てて、カーソルをカードの上に置いてください）`;
          return "カーソルをカードの上に置いて、親指と人差し指でつまんだまま待ってください";
        },
      };
    },
  },
  right_click: {
    task: "丸を右クリックして、出てきたメニューの「コピー」を選んでください",
    make(area, pass) {
      const SIZE = 90;
      const dot = el("div", { class: "test-target", style: `width:${SIZE}px;height:${SIZE}px` });
      const QUIPS = { 開く: "開くのはまた今度", 名前を変更: "名前はそのままで", 削除: "それは違います", ゴミ箱に入れる: "捨てないでください" };
      const menu = el("div", { class: "fake-menu hidden" }, ["開く", "コピー", "名前を変更", "削除", "ゴミ箱に入れる"].map((t) =>
        el("button", { onclick: (e) => {
          e.stopPropagation();
          menu.classList.add("hidden");
          if (t === "コピー") pass("合格: 右クリックしてメニューを選べました");
          else quip(area, QUIPS[t], inArea(area, e.clientX, e.clientY));
        } }, t)));
      area.replaceChildren(dot, menu, progress("右クリック → コピー"));
      const p = spot(area, SIZE);
      dot.style.left = `${p.x}px`; dot.style.top = `${p.y}px`;
      let opened = false;
      dot.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        opened = true;
        const r = area.getBoundingClientRect();
        menu.style.left = `${Math.min(e.clientX - r.left, r.width - 160)}px`; menu.style.top = `${Math.min(e.clientY - r.top, r.height - 220)}px`;
        menu.classList.remove("hidden");
      });
      area.addEventListener("contextmenu", (e) => e.preventDefault());
      return {
        hint(s) {
          if (opened) return "メニューの「コピー」に合わせて、親指と人差し指でつまんで離してください";
          if (s.mode === "right") return "親指と中指を付けたまま、もう少し待ってください";
          if (s.mode !== "cursor") return `今は「${modeName(s.mode)}」です（人差し指を立てて、カーソルを丸の上に置いてください）`;
          if (s.tm != null) return `親指と中指の距離: 今 ${pct(s.tm)}（${s.cfg.right_on} 未満で付いたと判定）`;
          return "";
        },
      };
    },
  },
  scroll: {
    task: "カーソルを枠の中に置いたまま、ピースで手を動かして、いちばん下の印まで下げ、いちばん上の印まで戻してください",
    make(area, pass) {
      const page = el("div", { class: "fake-scroll" }, el("div", { class: "scroll-mark" }, "いちばん上"),
        [...Array(40)].map((_, i) => el("p", { class: "muted" }, `${i + 1} 行目`)), el("div", { class: "scroll-mark" }, "いちばん下"));
      const prog = progress("下 —　上 —");
      area.replaceChildren(page, prog);
      let down = false, up = false;
      page.addEventListener("scroll", () => {
        const max = page.scrollHeight - page.clientHeight;
        if (!down && page.scrollTop >= max - 8) { down = true; quip(area, "折り返し地点です"); }
        if (up) return;
        if (down && page.scrollTop <= 8) up = true;
        prog.textContent = `下 ${down ? "✓" : "—"}　上 ${up ? "✓" : "—"}`;
        if (down && up) { quip(area, "ゴール！この感情をピースで表してください", null, true); pass("合格: 下まで下げて、上まで戻せました"); }
      });
      return {
        hint(s) {
          if (s.mode === "peace") return "ピースのまま、手を上下に動かしてください（大きく動かすと速く）";
          return peaceHint(s);
        },
      };
    },
  },
  nav: {
    task: "ピースで手を右へ動かして止めると「戻る」、左へ動かして止めると「進む」です（両方を 1 回ずつ、キーは送らず下の画面で見せます）",
    make(area, pass) {
      const pages = ["1ページ目", "2ページ目", "3ページ目"];
      let at = 1;
      const did = { back: false, forward: false };
      const view = el("div", { class: "fake-browser" });
      const status = progress("");
      area.replaceChildren(view, status);
      const draw = (dir) => {
        view.replaceChildren(el("div", { class: "fake-bar" }, el("span", { class: did.back ? "ok" : "" }, "‹ 戻る"),
          el("span", { class: did.forward ? "ok" : "" }, "進む ›"), el("span", { class: "muted" }, `example.com/${at + 1}`)),
        el("div", { class: `fake-page ${dir || ""}` }, pages[at]));
        status.textContent = `戻る ${did.back ? "✓" : "—"}　進む ${did.forward ? "✓" : "—"}`;
      };
      draw();
      return {
        onEvent(ev) {
          if (ev.kind !== "nav" || !ev.active) return;
          if (ev.dir === "back" && at === 0) quip(area, "これ以上戻れません");
          if (ev.dir === "back" && at > 0) at -= 1;
          if (ev.dir === "forward" && at < pages.length - 1) at += 1;
          did[ev.dir] = true;
          draw(ev.dir);
          if (did.back && did.forward) pass("合格: 戻る・進むを 1 回ずつできました");
        },
        hint(s) { return s.mode === "peace" ? "ピースになっています（手を左右に動かして、少し止めてください）" : peaceHint(s); },
      };
    },
  },
  desktop: {
    task: "いいねで手を左へ動かして止めると右のデスクトップへ、右へ動かして止めると左のデスクトップへ（両方を 1 回ずつ、キーは送らず下の画面で見せます）",
    make(area, pass) {
      area.replaceChildren();
      const d = desktops(area);
      const did = { left: false, right: false };
      const prog = progress("");
      area.append(prog);
      const draw = () => { prog.textContent = `右のデスクトップへ ${did.left ? "✓" : "—"}　左のデスクトップへ ${did.right ? "✓" : "—"}`; };
      draw();
      return {
        onEvent(ev) {
          if (ev.kind !== "thumbsup" || !ev.active || ev.dir === "up") return;
          d.set(d.at + (ev.dir === "left" ? 1 : -1)); // 手を左へ → 右のデスクトップ（⌃→）
          did[ev.dir] = true;
          draw();
          if (did.left && did.right) pass("合格: 左右のデスクトップへ 1 回ずつ移れました");
        },
        hint(s) { return s.mode === "thumbsup" ? "いいねになっています（手を左右に動かして、少し止めてください）" : thumbsHint(s); },
      };
    },
  },
  mission: {
    task: "いいねで手を上に動かして止めると Mission Control が開き、もう一度で閉じます（開いて閉じるまで、キーは送らず下の画面で見せます）",
    make(area, pass) {
      area.replaceChildren();
      const d = desktops(area);
      let open = false, opened = false;
      const prog = progress("開く —　閉じる —");
      // 一覧のときだけ見える小さなアイコン。カーソルを重ねると跳ねる（合格には関わらない）
      const secret = el("div", { class: "fake-secret", "aria-hidden": "true" }, "★");
      secret.addEventListener("mouseenter", () => {
        if (!open) return;
        secret.classList.remove("found"); void secret.offsetWidth; secret.classList.add("found");
        quip(area, "見つかった", inArea(area, secret.getBoundingClientRect().left, secret.getBoundingClientRect().top - 24));
      });
      d.view.append(secret);
      area.append(prog);
      return {
        onEvent(ev) {
          if (ev.kind !== "thumbsup" || !ev.active || ev.dir !== "up") return;
          open = !open;
          d.view.classList.toggle("overview", open);
          if (open) opened = true;
          prog.textContent = `開く ${opened ? "✓" : "—"}　閉じる ${opened && !open ? "✓" : "—"}`;
          if (opened && !open) pass("合格: Mission Control を開いて閉じられました");
        },
        hint(s) { return s.mode === "thumbsup" ? "いいねになっています（手を上に動かして、少し止めてください）" : thumbsHint(s); },
      };
    },
  },
  zoom: {
    task: "手の甲を向けて指を伸ばし、手招きで 1 段ズームイン、1 段ズームアウトしてください（キーは送らず、下の画面で見せます）",
    make(area, pass) {
      const levels = [50, 67, 80, 90, 100, 110, 125, 150, 175, 200];
      let at = 4;
      const did = { in: false, out: false };
      const content = el("div", { class: "zoom-content" }, el("h3", {}, "ズームの練習"), el("p", {}, "この文と図の大きさが、1 段ずつ変わります"),
        el("div", { class: "zoom-figure" }), el("p", { class: "zoom-secret" }, "ここまで拡大しましたね"));
      const view = el("div", { class: "zoom-view" }, content);
      const prog = progress("");
      area.replaceChildren(view, prog);
      const draw = () => { content.style.transform = `scale(${levels[at] / 100})`; content.classList.toggle("max", at === levels.length - 1); prog.textContent = `${levels[at]}%　イン ${did.in ? "✓" : "—"}　アウト ${did.out ? "✓" : "—"}`; };
      draw();
      return {
        onEvent(ev) {
          if (ev.kind !== "zoom" || !ev.active) return;
          at = Math.max(0, Math.min(levels.length - 1, at + (ev.dir === "in" ? 1 : -1)));
          did[ev.dir] = true;
          draw();
          if (did.in && did.out) pass("合格: ズームイン・ズームアウトを 1 段ずつできました");
        },
        hint(s) {
          if (s.mode === "zoom") return "ズームに入っています（手招きのように指先を遠ざけるとイン、近づけるとアウト）";
          if (s.palm_facing != null && s.palm_facing <= s.cfg.zoom_back_min) return `手の甲をカメラに向けてください（向き ${pct(s.palm_facing)}、${s.cfg.zoom_back_min} を超えると手の甲）`;
          return "指を伸ばし、親指を開いて、短時間保ってください";
        },
      };
    },
  },
  wake: {
    task: "次の 3 つを 1 回ずつ: 手のひらを向けてオン、手を隠して自動でオフ、右 Command でオン・オフ",
    ownHint: true, // オフの間も練習の対象なので、共通のヒント（オフです）を出さない
    make(area, pass) {
      const did = { palm: false, hand_absent: false, key: false };
      const lamp = el("div", { class: "wake-lamp" }, "—");
      const list = el("ul", { class: "tut-lines wake-list" });
      area.replaceChildren(el("div", { class: "wake-view" }, lamp, list));
      let last = null;
      const draw = (on) => {
        lamp.textContent = on ? "オン" : "オフ";
        lamp.classList.toggle("on", !!on);
        list.replaceChildren(...[["palm", "手のひらを向けてオン"], ["hand_absent", "手を隠して自動でオフ"], ["key", "右 Command で切り替え"]]
          .map(([k, t]) => el("li", { class: did[k] ? "ok" : "" }, `${did[k] ? "✓" : "—"}　${t}`)));
      };
      draw(false);
      return {
        onSnap(s) {
          const r = s.run || {};
          if (last !== null && r.active !== last && did[r.reason] !== undefined) {
            did[r.reason] = true;
            if (r.reason === "hand_absent" && !r.active) quip(area, "おやすみなさい");
            if (r.reason === "palm" && r.active) quip(area, "おはようございます");
          }
          last = r.active;
          draw(r.active);
          if (did.palm && did.hand_absent && did.key) pass("合格: 3 つの切り替えができました");
        },
        hint(s) {
          const r = s.run || {};
          if (!did.palm && !r.active) return "手のひらをカメラに向けて、5 本の指を開いて短時間保つとオン";
          if (!did.hand_absent && r.active) return "手をカメラの外に出して、しばらく待つと自動でオフ";
          if (!did.key) return "右 Command だけを押して離すと、オン・オフが切り替わります";
          return "";
        },
      };
    },
  },
};

async function init() {
  M = await (await fetch(`/tutorial/manifest.json?t=${encodeURIComponent(TOKEN)}`)).json();
  buildTree();
  $("btn-list").onclick = showList;
  $("btn-prev").onclick = () => openStep(cur - 1);
  $("btn-next").onclick = () => openStep(cur + 1);
  $("btn-test").onclick = startTest;
  $("done-mark").onclick = showDone;
  $("btn-done-close").onclick = () => $("done").classList.add("hidden");
  P = await api("/api/tutorial/progress").catch(() => P);
  showProgress();
  addEventListener("pagehide", stopTest); // 画面を閉じたら練習モードを抜ける
}
init().catch((e) => { $("tree").textContent = `読み込めませんでした: ${e.message}`; });
