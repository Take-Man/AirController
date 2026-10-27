// 画面の行き来（TASKS-20261027-package 第5節、作者の FB）: ホーム（起動）から、環境のチェック・チュートリアル・設定へ進み、
// 各画面の「ホームに戻る」で戻る。リンク（data-go）にトークンを付ける。
// アプリで別のウィンドウに開いた画面（設定・チュートリアル。URL に win=1）では、「ホームに戻る」を出さない
(() => {
  const q = new URLSearchParams(location.search);
  const t = q.get("t") || "";
  for (const a of document.querySelectorAll("a[data-go]")) a.href = `${a.dataset.go}?t=${encodeURIComponent(t)}`;
  if (q.get("win") === "1") for (const b of document.querySelectorAll(".backbar")) b.remove();
})();
