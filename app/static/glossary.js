"use strict";

/* ---------- 工具 ---------- */
const $ = (id) => document.getElementById(id);
const cps = (s) => Array.from(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function showBanner(msg, isError = false) {
  const b = $("banner");
  b.textContent = msg;
  b.className = "banner" + (isError ? " error" : "");
}
function hideBanner() { $("banner").className = "banner hidden"; }

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!resp.ok) {
    const err = new Error("HTTP " + resp.status);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

/* ---------- 登记输入解析 ----------
 * 每行：页号 会话号@版本（页号与会话号之间可用空白或冒号/逗号分隔）；
 * 空行忽略，格式错误的行在本地先提示，不发请求。 */
function parseEntries(text) {
  const out = [];
  const errors = [];
  text.split(/\r?\n/).forEach((line, idx) => {
    const raw = line.trim();
    if (!raw) return;
    const m = raw.match(/^(\d+)[\s,，:：]+([^\s@]+)\s*@\s*(\d+)$/);
    if (!m) {
      errors.push(`第 ${idx + 1} 行无法解析：${raw}`);
      return;
    }
    out.push({ page: Number(m[1]), session_id: m[2], revision: Number(m[3]) });
  });
  return { entries: out, errors };
}

/* ---------- 渲染 ---------- */
function shortSid(sid) { return sid.slice(0, 8) + "…"; }

function renderPages(data) {
  const body = $("pages-body");
  body.innerHTML = "";
  $("pages-summary").textContent = data.pages.length
    ? `已登记 ${data.pages.length} / ${data.max_pages} 页；` +
      `有效 ${data.pages.filter(p => p.status === "active").length} 页，` +
      `待补录 ${data.pending.length} 页。`
    : "尚未登记任何页。";
  $("pages-table").classList.toggle("hidden", data.pages.length === 0);

  for (const p of data.pages) {
    const tr = document.createElement("tr");
    const badge = p.status === "active"
      ? `<span class="badge auto">有效</span>`
      : `<span class="badge impossible">待补录</span>`;
    // 待补录页填入会话当前版本，便于用户重新确认后直接补录
    const fillRev = p.status === "pending" && p.current_revision != null
      ? p.current_revision : p.revision;
    tr.innerHTML = `
      <td class="num">${p.page}</td>
      <td class="mono" title="${esc(p.session_id)}">${esc(shortSid(p.session_id))}</td>
      <td class="mono">r${p.revision}</td>
      <td>${badge}${p.status === "pending"
        ? `<div class="hint">${esc(p.message)}</div>` : ""}</td>
      <td class="mono">${esc(p.updated_at)}</td>
      <td><button type="button" class="linkish" data-fill="${p.page}"
        data-sid="${esc(p.session_id)}" data-rev="${fillRev}"
        >${p.status === "pending" ? "填入补录行" : "填入登记行"}</button></td>`;
    body.appendChild(tr);
  }
  body.querySelectorAll("button[data-fill]").forEach((btn) => {
    btn.addEventListener("click", () => {
      appendEntry(btn.dataset.fill, btn.dataset.sid, btn.dataset.rev);
    });
  });
}

function appendEntry(page, sid, rev) {
  const lines = $("entry-input").value.split(/\r?\n/);
  // 同一页已有旧行时替换掉，避免用户误留旧版本
  const kept = lines.filter((l) => l.trim() &&
    !new RegExp(`^\\s*${page}[\\s,，:：]`).test(l));
  kept.push(`${page} ${sid}@${rev}`);
  $("entry-input").value = kept.join("\n") + "\n";
  $("entry-input").focus();
}

function renderPending(data) {
  const box = $("pending-list");
  box.innerHTML = "";
  $("pending-summary").textContent = data.pending.length
    ? `${data.pending.length} 页版本的确认结果已不可用，已退出计数，请补录。`
    : "没有待补录的页。";
  for (const p of data.pending) {
    const div = document.createElement("div");
    div.className = "pending-card";
    div.innerHTML = `
      <span class="page-chip">页 ${p.page}</span>
      <span class="mono">${esc(shortSid(p.session_id))}</span>
      <span class="hint">登记 r${p.revision}` +
      (p.current_revision != null ? ` → 当前 r${p.current_revision}` : "") + `</span>
      <span class="pending-msg">${esc(p.message)}</span>` +
      (p.reason === "session_not_found"
        ? ""
        : `<button type="button" class="linkish" data-pending-page="${p.page}"
             data-sid="${esc(p.session_id)}"
             data-rev="${p.current_revision ?? p.revision}">填写补录行</button>`);
    box.appendChild(div);
  }
  box.querySelectorAll("button[data-pending-page]").forEach((btn) => {
    btn.addEventListener("click", () => {
      // 填入同一页号 + 当前版本；用户确认会话已重新确认后再整批提交
      appendEntry(btn.dataset.pendingPage, btn.dataset.sid, btn.dataset.rev);
    });
  });
}

function contextHtml(revised, start, end) {
  const arr = cps(revised);
  const from = Math.max(0, start - 12), to = Math.min(arr.length, end + 12);
  return (from > 0 ? "…" : "") +
    esc(arr.slice(from, start).join("")) + "<mark>" +
    esc(arr.slice(start, end).join("")) + "</mark>" +
    esc(arr.slice(end, to).join("")) + (to < arr.length ? "…" : "");
}

async function renderTerms(data) {
  const box = $("term-cards");
  box.innerHTML = "";
  $("terms-summary").textContent = data.terms.length
    ? `共 ${data.terms.length} 个候选词（至少 ${data.min_support} 页支持）：` +
      data.terms.map(t => `${t.text}(${t.support})`).join("、")
    : `暂无候选词；候选词须至少有 ${data.min_support} 个不同页面支持。`;

  for (const term of data.terms) {
    const card = document.createElement("div");
    card.className = "term-card";
    let html = `<div class="term-head">
      <span class="term-text">${esc(term.text)}</span>
      <span class="badge auto">${term.support} 页支持</span>
      <span class="hint">页 ${term.pages.join("、")}</span>
    </div><table class="evidence"><thead><tr>
      <th>页号</th><th>会话 / 版本</th><th>标注区间（修订文码点）</th>
      <th>修订文上下文</th></tr></thead><tbody>`;
    for (const ev of term.evidence) {
      const ctx = typeof ev.revised === "string"
        ? contextHtml(ev.revised, ev.start, ev.end)
        : `<span class="hint">上下文不可用</span>`;
      html += `<tr>
        <td class="num">${ev.page}</td>
        <td class="mono">${esc(shortSid(ev.session_id))} · r${ev.revision}</td>
        <td class="mono">[${ev.start}, ${ev.end})</td>
        <td class="evidence-text">${ctx}</td>
      </tr>`;
    }
    html += `</tbody></table>`;
    card.innerHTML = html;
    box.appendChild(card);
  }
}

async function load() {
  const data = await api("/api/glossary");
  $("max-pages").textContent = data.max_pages;
  $("list-info").textContent =
    `${data.page_count} / ${data.max_pages} 页已登记 · ` +
    `${data.terms.length} 个候选词 · ${data.pending.length} 页待补录`;
  renderPages(data);
  renderPending(data);
  await renderTerms(data);
  return data;
}

/* ---------- 登记 ---------- */
async function register() {
  hideBanner();
  $("reject-detail").classList.add("hidden");
  $("reject-detail").innerHTML = "";
  const parsed = parseEntries($("entry-input").value);
  if (parsed.errors.length) {
    showBanner("请先修正登记格式：" + parsed.errors.join("；"), true);
    return;
  }
  if (!parsed.entries.length) {
    showBanner("请至少填写一页。", true);
    return;
  }

  $("register-status").textContent = "提交中…";
  try {
    await api("/api/glossary/pages", {
      method: "POST",
      body: JSON.stringify({ entries: parsed.entries }),
    });
    $("register-status").textContent = "登记成功，词表已刷新。";
    await load();
  } catch (e) {
    $("register-status").textContent = "";
    const d = e.data && e.data.detail;
    if (e.status === 409 && d && d.errors) {
      showBanner("整批登记被拒绝（涉及页 " + (d.pages || []).join("、") +
                 "），原有登记保持不变。", true);
      const box = $("reject-detail");
      box.innerHTML = "<ul>" + d.errors.map((err) =>
        `<li><strong>页 ${err.pages.join("、")}</strong>：${esc(err.message)}</li>`
      ).join("") + "</ul>";
      box.classList.remove("hidden");
      await load();  // 保证页面显示的是未被本次提交改变的原登记
    } else {
      showBanner("登记失败：" + (typeof d === "string" ? d :
        (d && d.message) || JSON.stringify(d)), true);
    }
  }
}

/* ---------- 启动 ---------- */
function init() {
  $("register").addEventListener("click", register);

  // 从标注迁移页跳转过来时，预填当前会话与版本
  const params = new URLSearchParams(location.search);
  const sid = params.get("session");
  const rev = params.get("revision");
  if (sid && rev) {
    $("entry-input").value = `${sid}@${rev}`;
    $("entry-input").focus();
    $("prefill-hint").textContent =
      "已预填当前会话与版本，请在行首补上扫描页号。";
  }

  load().catch((e) => showBanner("加载失败：" + e.message, true));
}

document.addEventListener("DOMContentLoaded", init);
