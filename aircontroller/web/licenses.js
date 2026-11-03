// 同梱したライブラリのライセンス: pkg/licenses/collect.sh が作った全文（/licenses.txt）をそのまま出す
"use strict";
(async () => {
  const r = await fetch("/licenses.txt");
  document.getElementById("notices").textContent = await r.text();
})().catch((e) => { document.getElementById("notices").textContent = `読み込めませんでした: ${e.message}`; });
