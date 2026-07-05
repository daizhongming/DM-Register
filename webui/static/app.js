// 团子喵的 WebUI 交互逻辑 ~

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// ──────────────────────── 工具 ────────────────────────

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(formatApiError(data.detail) || resp.statusText);
  return data;
}

function formatApiError(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((x) => {
      const loc = Array.isArray(x.loc) ? x.loc.slice(1).join(".") : "";
      return `${loc ? loc + ": " : ""}${x.msg || JSON.stringify(x)}`;
    }).join("; ");
  }
  return detail.message || detail.msg || JSON.stringify(detail);
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

function logLine(text, kind = "") {
  const box = $("#logBox");
  const div = document.createElement("div");
  div.className = "line " + kind;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function classifyLog(line) {
  const l = line.toLowerCase();
  if (l.includes("error") || l.includes("失败") || l.includes("拒绝")) return "err";
  if (l.includes("warning") || l.includes("warn")) return "warn";
  if (l.includes("成功") || l.includes("完成") || l.includes("命中") || l.includes("ok")) return "ok";
  return "";
}

// ──────────────────────── 统计栏 ────────────────────────

async function refreshStats() {
  try {
    const { stats } = await api("/api/stats");
    const items = [
      { v: stats.total,     cls: "" },
      { v: stats.available, cls: "ok" },
      { v: stats.in_use,    cls: "warn" },
      { v: stats.done,      cls: "done" },
      { v: stats.failed,    cls: "bad" },
    ];
    $$("#statsBar .pill").forEach((el, i) => {
      el.querySelector("b").textContent = items[i].v;
    });
  } catch (e) {
    console.error("stats:", e);
  }
}

// ──────────────────────── 导入 ────────────────────────

$("#btnImport").addEventListener("click", async () => {
  const text = $("#importText").value.trim();
  if (!text) {
    $("#importResult").textContent = "请输入要导入的接码号";
    return;
  }
  $("#btnImport").disabled = true;
  $("#importResult").textContent = "导入中...";
  try {
    const r = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    $("#importResult").textContent =
      `✅ 解析 ${r.parsed} 行，新增 ${r.inserted}，更新 ${r.updated}，跳过 ${r.skipped}`;
    $("#importResult").className = "result ok";
    $("#importText").value = "";
    refreshStats();
    refreshPool();
  } catch (e) {
    $("#importResult").textContent = "❌ " + e.message;
    $("#importResult").className = "result bad";
  } finally {
    $("#btnImport").disabled = false;
  }
});

// ──────────────────────── 触发注册 ────────────────────────

let currentEs = null;
let currentRunId = null;
let sub2apiScanRows = [];

function ensureSub2apiScanActions(box) {
  const head = box?.querySelector(".scan-results-head");
  if (!head || $("#btnSub2apiDeleteSelected")) return;
  head.insertAdjacentHTML("beforeend", `
    <button id="btnSub2apiSelectMissing" type="button">选择不可自动授权</button>
    <button id="btnSub2apiInvertSelection" type="button">反选</button>
    <button id="btnSub2apiReauthSelected" type="button" disabled>授权选中</button>
    <button id="btnSub2apiDeleteSelected" type="button" disabled>删除选中</button>
    <button id="btnSub2apiCancelRun" type="button">终止扫描</button>
    <span id="sub2apiDeleteResult" class="result"></span>
  `);
}

function ensureSub2apiScanResults() {
  let box = $("#sub2apiScanResults");
  if (box) {
    ensureSub2apiScanActions(box);
    return box;
  }

  const row = $("#sub2apiReauthResult")?.closest(".row");
  if (!row) return null;
  box = document.createElement("div");
  box.id = "sub2apiScanResults";
  box.className = "scan-results hidden";
  box.innerHTML = `
    <div class="scan-results-head">
      <b>异常账号列表</b>
      <span id="sub2apiScanSummary" class="result"></span>
      <button id="btnSub2apiSelectMissing" type="button">选择不可自动授权</button>
      <button id="btnSub2apiInvertSelection" type="button">反选</button>
      <button id="btnSub2apiReauthSelected" type="button" disabled>授权选中</button>
      <button id="btnSub2apiDeleteSelected" type="button" disabled>删除选中</button>
      <button id="btnSub2apiCancelRun" type="button">终止扫描</button>
      <span id="sub2apiDeleteResult" class="result"></span>
    </div>
    <table id="sub2apiScanTable">
      <thead>
        <tr><th>ID</th><th>邮箱</th><th>异常原因</th><th>授权结果</th><th>邮箱接码</th><th>PayMesh卡</th><th>本地RT</th><th>手机号记录</th><th>短信可用</th><th>自动授权判定</th><th>阻断原因</th></tr>
      </thead>
      <tbody></tbody>
    </table>
  `;
  row.parentElement.insertAdjacentElement("afterend", box);
  ensureSub2apiScanActions(box);
  return box;
}

function resetSub2apiScanResults() {
  const box = ensureSub2apiScanResults();
  if (!box) return;
  sub2apiScanRows = [];
  box.classList.add("hidden");
  $("#sub2apiScanSummary").textContent = "";
  $("#sub2apiScanTable tbody").innerHTML = "";
  const result = $("#sub2apiDeleteResult");
  if (result) {
    result.textContent = "";
    result.className = "result";
  }
  updateSub2apiDeleteButton();
}

function ensureSub2apiScanHeader() {
  const head = $("#sub2apiScanTable thead tr");
  if (!head || $("#sub2apiScanSelectAll")) return;
  const th = document.createElement("th");
  th.innerHTML = `<input id="sub2apiScanSelectAll" type="checkbox" title="选择当前列表">`;
  head.insertBefore(th, head.firstChild);
}

function selectedSub2apiAccountIds() {
  return Array.from($$(".sub2api-scan-check:checked"))
    .map((x) => parseInt(x.dataset.accountId || "", 10))
    .filter(Number.isFinite);
}

function updateSub2apiDeleteButton() {
  const ids = selectedSub2apiAccountIds();
  const reauthBtn = $("#btnSub2apiReauthSelected");
  if (reauthBtn) reauthBtn.disabled = ids.length === 0;
  const btn = $("#btnSub2apiDeleteSelected");
  if (btn) btn.disabled = ids.length === 0;
  const all = $("#sub2apiScanSelectAll");
  if (all) {
    const checks = Array.from($$(".sub2api-scan-check:not(:disabled)"));
    all.checked = checks.length > 0 && checks.every((x) => x.checked);
    all.indeterminate = checks.some((x) => x.checked) && !all.checked;
  }
  const result = $("#sub2apiDeleteResult");
  if (result) {
    result.textContent = ids.length ? `已选 ${ids.length} 个` : "";
    result.className = "result";
  }
}

function renderSub2apiScanResults(items) {
  const box = ensureSub2apiScanResults();
  if (!box) return;
  const rows = Array.isArray(items) ? items : [];
  sub2apiScanRows = rows;
  ensureSub2apiScanHeader();
  const tb = $("#sub2apiScanTable tbody");
  tb.innerHTML = "";
  box.classList.remove("hidden");
  const unavailable = rows.filter((r) => r.can_attempt_reauth === false).length;
  $("#sub2apiScanSummary").textContent = rows.length
    ? `共 ${rows.length} 个异常账号，不可自动授权 ${unavailable} 个`
    : "未发现异常账号";

  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 12;
    td.className = "muted-cell";
    td.textContent = "未发现异常账号";
    tr.appendChild(td);
    tb.appendChild(tr);
    updateSub2apiDeleteButton();
    return;
  }

  const add = (tr, value) => {
    const td = document.createElement("td");
    td.textContent = value == null || value === "" ? "-" : String(value);
    tr.appendChild(td);
  };
  const yesNo = (v) => v ? "有" : "无";
  const listText = (v) => Array.isArray(v) ? v.join(", ") : (v || "");
  for (const r of rows) {
    const res = r.auth_resources || {};
    const tr = document.createElement("tr");
    if (r.can_attempt_reauth === false) tr.classList.add("missing-local");
    if (r.ok === true) tr.classList.add("reauth-ok");
    if (r.ok === false) tr.classList.add("reauth-failed");
    const pick = document.createElement("td");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "sub2api-scan-check";
    check.dataset.accountId = String(r.account_id || "");
    check.disabled = !r.account_id;
    pick.appendChild(check);
    tr.appendChild(pick);
    add(tr, r.account_id);
    add(tr, r.email);
    add(tr, r.reason);
    add(tr, r.ok === true
      ? `成功${r.method ? ` (${r.method})` : ""}`
      : (r.ok === false ? `失败: ${r.error || "unknown"}` : ("ok" in r ? "未执行" : "-")));
    add(tr, res.mail_source || (r.has_outlook ? "outlook" : ""));
    add(tr, res.paymesh_card || "");
    add(tr, yesNo(r.has_registered_rt));
    add(tr, res.phone_number || "");
    add(tr, yesNo(r.sms_available));
    add(tr, r.can_attempt_reauth === false ? "不可自动授权" : (r.reauth_method_hint || r.method || "可授权"));
    add(tr, listText(r.blockers));
    tb.appendChild(tr);
  }
  updateSub2apiDeleteButton();
}

const sub2apiScanBox = ensureSub2apiScanResults();
if (sub2apiScanBox) {
  sub2apiScanBox.addEventListener("change", (e) => {
    if (e.target.id === "sub2apiScanSelectAll") {
      $$(".sub2api-scan-check:not(:disabled)").forEach((x) => { x.checked = e.target.checked; });
    }
    if (e.target.id === "sub2apiScanSelectAll" || e.target.classList.contains("sub2api-scan-check")) {
      updateSub2apiDeleteButton();
    }
  });

  sub2apiScanBox.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;

    if (btn.id === "btnSub2apiSelectMissing") {
      $$(".sub2api-scan-check:not(:disabled)").forEach((x) => {
        const row = sub2apiScanRows.find((r) => String(r.account_id || "") === x.dataset.accountId);
        x.checked = row ? row.can_attempt_reauth === false : false;
      });
      updateSub2apiDeleteButton();
      return;
    }

    if (btn.id === "btnSub2apiInvertSelection") {
      $$(".sub2api-scan-check:not(:disabled)").forEach((x) => { x.checked = !x.checked; });
      updateSub2apiDeleteButton();
      return;
    }

    if (btn.id === "btnSub2apiReauthSelected") {
      await runSub2apiAccountCheck({ currentTarget: btn }, false, true);
      return;
    }

    if (btn.id === "btnSub2apiCancelRun") {
      if (!currentRunId) {
        alert("当前没有正在运行的扫描/授权任务");
        return;
      }
      btn.disabled = true;
      const result = $("#sub2apiDeleteResult");
      if (result) result.textContent = "正在终止...";
      try {
        await api(`/api/runs/${currentRunId}/cancel`, { method: "POST" });
        $("#sub2apiReauthResult").textContent = "cancel requested";
        if (result) {
          result.textContent = "已请求终止";
          result.className = "result warn";
        }
      } catch (err) {
        if (result) {
          result.textContent = "终止失败: " + err.message;
          result.className = "result bad";
        }
      } finally {
        btn.disabled = false;
      }
      return;
    }

    if (btn.id !== "btnSub2apiDeleteSelected") return;
    const ids = selectedSub2apiAccountIds();
    if (!ids.length) return;
    if (!confirm(`确定删除选中的 ${ids.length} 个 SUB2API 账号？\n只删除已勾选的账号，不会删除本地凭据。`)) return;

    const result = $("#sub2apiDeleteResult");
    btn.disabled = true;
    if (result) result.textContent = "删除中...";
    try {
      const r = await api("/api/sub2api/accounts/bulk_delete", {
        method: "POST",
        body: JSON.stringify({ account_ids: ids }),
      });
      const deleted = new Set((r.results || []).filter((x) => x.ok).map((x) => Number(x.account_id)));
      if (deleted.size) {
        renderSub2apiScanResults(sub2apiScanRows.filter((x) => !deleted.has(Number(x.account_id))));
      }
      if (result) {
        result.textContent = r.failed
          ? `已删除 ${r.deleted} 个，失败 ${r.failed} 个`
          : `已删除 ${r.deleted} 个`;
        result.className = "result " + (r.failed ? "bad" : "ok");
      }
    } catch (err) {
      if (result) {
        result.textContent = "删除失败: " + err.message;
        result.className = "result bad";
      }
    } finally {
      const savedText = result?.textContent || "";
      const savedClass = result?.className || "result";
      updateSub2apiDeleteButton();
      if (result && savedText) {
        result.textContent = savedText;
        result.className = savedClass;
      }
    }
  });
}

$("#btnRun").addEventListener("click", async () => {
  const email = $("#regEmail").value.trim();
  const opts = {
    email: email || null,
    proxy: $("#regProxy").value.trim(),
    otp_timeout: parseInt($("#regOtpTimeout").value || "180", 10),
    want_access_token: true,
    want_session_token: true,
    want_refresh_token: true,
  };
  $("#btnRun").disabled = true;
  $("#runStatus").textContent = "启动中...";
  $("#runStatus").className = "result";
  $("#logBox").innerHTML = "";

  try {
    const r = await api("/api/register", {
      method: "POST",
      body: JSON.stringify(opts),
    });
    $("#runStatus").textContent = `🚀 已启动 run_id=${r.run_id} email=${r.email}`;
    logLine(`[client] 启动注册 run_id=${r.run_id} email=${r.email}`, "evt");
    streamRun(r.run_id);
  } catch (e) {
    $("#runStatus").textContent = "❌ " + e.message;
    $("#runStatus").className = "result bad";
    $("#btnRun").disabled = false;
  }
});

function streamRun(runId) {
  if (currentEs) { try { currentEs.close(); } catch (_) {} }
  const es = new EventSource(`/api/runs/${runId}/stream`);
  currentEs = es;
  currentRunId = runId;

  es.addEventListener("log", (e) => {
    try {
      const d = JSON.parse(e.data);
      if (!d.line) return;
      logLine(d.line, classifyLog(d.line));
    } catch (_) {}
  });

  es.addEventListener("status", (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.kind === "done" && d.task === "sub2api_reauth") {
        const s = d.cancelled
          ? `SUB2API task cancelled: candidates=${d.candidates || 0}, results=${(d.results || []).length}`
          : d.dry_run
          ? `SUB2API account check done: candidates=${d.candidates || 0}, checked=${d.checked || 0}`
          : `SUB2API 401 reauth done: success=${d.success || 0}, failed=${d.failed || 0}, candidates=${d.candidates || 0}`;
        $("#runStatus").innerHTML = `<span class="${d.cancelled || (d.failed || 0) ? "warn" : "ok"}">${s}</span>`;
        $("#sub2apiReauthResult").textContent = s;
        $("#sub2apiReauthResult").className = "result " + (d.cancelled ? "warn" : ((d.failed || 0) ? "bad" : "ok"));
        renderSub2apiScanResults(d.results || []);
        logLine("[client] " + s, "evt");
        return;
      }
      if (d.kind === "done") {
        const s = `✅ 注册完成: access_token=${d.access_token_len}${d.partial ? "  (部分凭证)" : ""}`;
        const buttons = [];
        if (d.access_token_len > 0)  buttons.push(`<button class="quick-copy" data-email="${d.email}" data-field="access_token">📋 复制 access_token</button>`);
        $("#runStatus").innerHTML = `<span class="ok">${s}</span>${buttons.length ? "<br>" + buttons.join(" ") : ""}`;
        logLine("[client] " + s, "evt");
      } else if (d.kind === "error") {
        $("#runStatus").textContent = "❌ " + d.message;
        $("#runStatus").className = "result bad";
        logLine("[client] ❌ " + d.message, "err");
      } else if (d.kind === "phase") {
        if (d.phase === "sub2api_reauth" && d.message) {
          $("#sub2apiReauthResult").textContent = d.message;
        }
        logLine(`[client] ${d.message || `phase=${d.phase} email=${d.email || ""}`}`, "evt");
      }
    } catch (_) {}
  });

  es.addEventListener("end", () => {
    try { es.close(); } catch (_) {}
    currentEs = null;
    currentRunId = null;
    $("#btnRun").disabled = false;
    refreshStats();
    refreshPool();
    refreshRegistered();
    refreshRuns();
  });

  es.onerror = () => {
    try { es.close(); } catch (_) {}
    currentEs = null;
    currentRunId = null;
    $("#btnRun").disabled = false;
  };
}

// 状态栏快捷复制按钮（注册完成后直接显示在这里，不用切 Tab）
$("#runStatus").addEventListener("click", async (e) => {
  const copyBtn = e.target.closest("button.quick-copy");
  if (copyBtn) {
    const email = copyBtn.dataset.email;
    const field = copyBtn.dataset.field;
    try {
      const cred = await _loadCred(email);
      const val = cred[field] || "";
      if (!val) { alert(`${field} 为空`); return; }
      await _copyText(val, copyBtn);
    } catch (err) { alert("加载凭证失败: " + err.message); }
  }
});

// ──────────────────────── Tabs ────────────────────────

$$(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $$(".tab-content").forEach((c) => c.classList.add("hidden"));
    $("#tab-" + t.dataset.tab).classList.remove("hidden");
    if (t.dataset.tab === "registered") refreshRegistered();
    if (t.dataset.tab === "runs") refreshRuns();
    if (t.dataset.tab === "mailcfg") loadMailConfig();
    if (t.dataset.tab === "smscfg") loadSmsConfig();
    if (t.dataset.tab === "exportcfg") loadExportConfig();
  });
});

// ──────────────────────── 号池列表 ────────────────────────

async function refreshPool() {
  const status = $("#poolFilter").value;
  const { items } = await api(`/api/accounts?status=${encodeURIComponent(status)}`);
  const tb = $("#poolTable tbody");
  tb.innerHTML = "";
  for (const r of items) {
    const tr = document.createElement("tr");
    const canReset = (r.status === "done" || r.status === "failed" || r.status === "skipped");
    tr.innerHTML = `
      <td><input type="checkbox" class="pool-check" data-email="${r.email}"></td>
      <td>${r.email}</td>
      <td><span class="status ${r.status}">${r.status}</span></td>
      <td title="${r.fail_reason || ''}">${(r.fail_reason || '').slice(0, 50)}</td>
      <td>
        <button data-act="use" data-email="${r.email}">使用</button>
        ${canReset ? `<button data-act="reset" data-email="${r.email}" title="改回 available 重新注册">🔄 重置</button>` : ""}
        <button data-act="del" data-email="${r.email}">删除</button>
      </td>
    `;
    tb.appendChild(tr);
  }
  $("#poolSelectAll").checked = false;
  _updateSelCount();
}
$("#btnRefreshPool").addEventListener("click", refreshPool);
$("#poolFilter").addEventListener("change", refreshPool);

$("#btnResetFailed").addEventListener("click", async () => {
  if (!confirm("把所有 failed 号重置为 available？")) return;
  $("#poolActionResult").textContent = "处理中...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/reset_failed", { method: "POST" });
    $("#poolActionResult").textContent = `✅ 重置 ${r.reset} 个号为 available`;
    $("#poolActionResult").className = "result ok";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

$("#btnReleaseStale").addEventListener("click", async () => {
  $("#poolActionResult").textContent = "处理中...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/release_stale", { method: "POST" });
    $("#poolActionResult").textContent = `✅ 释放 ${r.released} 个卡死号`;
    $("#poolActionResult").className = "result ok";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

// ── 号池：复选框选择 + 批量删除 ──

function _selectedEmails() {
  return Array.from(document.querySelectorAll(".pool-check:checked"))
    .map(c => c.dataset.email);
}
function _updateSelCount() {
  const n = _selectedEmails().length;
  $("#selCount").textContent = n;
  $("#selCount2").textContent = n;
  $("#btnDeleteSelected").disabled = n === 0;
  $("#btnResetSelected").disabled = n === 0;
}
$("#poolTable").addEventListener("change", (e) => {
  if (e.target.classList.contains("pool-check")) _updateSelCount();
});
$("#poolSelectAll").addEventListener("change", (e) => {
  document.querySelectorAll(".pool-check").forEach(c => c.checked = e.target.checked);
  _updateSelCount();
});

$("#btnResetSelected").addEventListener("click", async () => {
  const emails = _selectedEmails();
  if (!emails.length) return;
  if (!confirm(`重置选中的 ${emails.length} 个号为 available？\n（号会重新可用，已保存的凭证不变）`)) return;
  $("#poolActionResult").textContent = "重置中...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/bulk_reset", {
      method: "POST",
      body: JSON.stringify({ emails }),
    });
    $("#poolActionResult").textContent = `✅ 已重置 ${r.reset} 个号`;
    $("#poolActionResult").className = "result ok";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

$("#btnDeleteSelected").addEventListener("click", async () => {
  const emails = _selectedEmails();
  if (!emails.length) return;
  if (!confirm(`确定删除选中的 ${emails.length} 个号？(不可恢复)`)) return;
  $("#poolActionResult").textContent = "删除中...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ emails }),
    });
    $("#poolActionResult").textContent = `✅ 已删除 ${r.deleted} 个号`;
    $("#poolActionResult").className = "result ok";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

$("#btnBulkDelStatus").addEventListener("click", async () => {
  const status = $("#bulkDelStatus").value;
  if (!status) {
    $("#poolActionResult").textContent = "请先选择要删除的状态";
    $("#poolActionResult").className = "result bad";
    return;
  }
  const tip = status === "all"
    ? "⚠️ 这会删除号池里所有号（含未注册的），确定？"
    : `确定删除全部 ${status} 状态的号？`;
  if (!confirm(tip)) return;
  $("#poolActionResult").textContent = "删除中...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ status }),
    });
    $("#poolActionResult").textContent = `✅ 已删除 ${r.deleted} 个 ${status} 号`;
    $("#poolActionResult").className = "result ok";
    $("#bulkDelStatus").value = "";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

$("#poolTable").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const email = btn.dataset.email;
  if (btn.dataset.act === "use") {
    $("#regEmail").value = email;
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else if (btn.dataset.act === "reset") {
    if (!confirm(`重置 ${email} 为 available？\n（号会重新可用，但已保存的凭证不变）`)) return;
    try {
      await api(`/api/accounts/reset/${encodeURIComponent(email)}`, { method: "POST" });
      refreshPool();
      refreshStats();
    } catch (err) {
      alert("重置失败: " + err.message);
    }
  } else if (btn.dataset.act === "del") {
    if (!confirm(`删除 ${email}？`)) return;
    await api(`/api/accounts/${encodeURIComponent(email)}`, { method: "DELETE" });
    refreshPool();
    refreshStats();
  }
});

// ──────────────────────── 注册结果列表 ────────────────────────

async function refreshRegistered() {
  const { items } = await api("/api/registered");
  const filter = document.querySelector("input[name='regFilter']:checked")?.value || "all";

  // 按筛选条件过滤
  let filtered = items;
  if (filter === "has_rt") {
    filtered = items.filter(r => r.rt_len > 0);
  } else if (filter === "no_rt") {
    filtered = items.filter(r => r.rt_len === 0);
  }

  const tb = $("#regTable tbody");
  tb.innerHTML = "";
  for (const r of filtered) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="checkbox" class="reg-check" data-email="${r.email}"></td>
      <td>${r.email}</td>
      <td>${r.at_len > 0 ? `<button class="copy-cell" data-email="${r.email}" data-field="access_token" title="点击复制 access_token">✅ ${r.at_len} 📋</button>` : "—"}</td>
      <td>${r.st_len > 0 ? `<button class="copy-cell" data-email="${r.email}" data-field="session_token" title="点击复制 session_token">✅ ${r.st_len} 📋</button>` : "—"}</td>
      <td>${r.rt_len > 0 ? `<button class="copy-cell" data-email="${r.email}" data-field="refresh_token" title="点击复制 refresh_token">✅ ${r.rt_len} 📋</button>` : "—"}</td>
      <td>${fmtTime(r.created_at)}</td>
      <td>
        <button data-act="view" data-email="${r.email}">查看凭证</button>
        <button data-act="del" data-email="${r.email}">删除</button>
      </td>
    `;
    tb.appendChild(tr);
  }
  $("#regSelectAll").checked = false;
  _updateSelCountReg();
}
$("#btnRefreshReg").addEventListener("click", refreshRegistered);

// radio 切换时自动刷新
document.querySelectorAll("input[name='regFilter']").forEach(r => {
  r.addEventListener("change", refreshRegistered);
});

// ── 注册结果：复选框 + 批量删 + 单行删 ──

function _selectedRegEmails() {
  return Array.from(document.querySelectorAll(".reg-check:checked")).map(c => c.dataset.email);
}
function _updateSelCountReg() {
  const n = _selectedRegEmails().length;
  $("#selCountReg").textContent = n;
  $("#btnDeleteSelectedReg").disabled = n === 0;
}
$("#regTable").addEventListener("change", (e) => {
  if (e.target.classList.contains("reg-check")) _updateSelCountReg();
});
$("#regSelectAll").addEventListener("change", (e) => {
  document.querySelectorAll(".reg-check").forEach(c => c.checked = e.target.checked);
  _updateSelCountReg();
});

$("#btnDeleteSelectedReg").addEventListener("click", async () => {
  const emails = _selectedRegEmails();
  if (!emails.length) return;
  if (!confirm(`确定删除选中的 ${emails.length} 条凭证？(不可恢复)`)) return;
  $("#exportResult").textContent = "删除中...";
  $("#exportResult").className = "result";
  try {
    const r = await api("/api/registered/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ emails }),
    });
    $("#exportResult").textContent = `✅ 已删除 ${r.deleted} 条凭证`;
    $("#exportResult").className = "result ok";
    refreshRegistered();
  } catch (e) {
    $("#exportResult").textContent = "❌ " + e.message;
    $("#exportResult").className = "result bad";
  }
});

$("#btnDeleteAllReg").addEventListener("click", async () => {
  if (!confirm("⚠️ 这会清空注册结果表里的所有凭证！\n确定继续？（号池不受影响）")) return;
  if (!confirm("再次确认：真的要删除全部凭证吗？此操作不可恢复！")) return;
  $("#exportResult").textContent = "清空中...";
  $("#exportResult").className = "result";
  try {
    const r = await api("/api/registered/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ all: true }),
    });
    $("#exportResult").textContent = `✅ 已清空 ${r.deleted} 条凭证`;
    $("#exportResult").className = "result ok";
    refreshRegistered();
  } catch (e) {
    $("#exportResult").textContent = "❌ " + e.message;
    $("#exportResult").className = "result bad";
  }
});

// 缓存最近查看的凭证（用于"复制全部 JSON"按钮和单字段复制）
let _credCache = null;

async function _loadCred(email) {
  if (_credCache && _credCache.email === email) return _credCache;
  const { data } = await api(`/api/registered/${encodeURIComponent(email)}`);
  _credCache = data;
  return data;
}

async function _copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    if (btn) {
      const orig = btn.textContent;
      const cls = btn.className;
      btn.textContent = "✅ 已复制";
      btn.className = cls + " copied";
      setTimeout(() => { btn.textContent = orig; btn.className = cls; }, 1200);
    }
  } catch (e) {
    alert("复制失败: " + e.message);
  }
}

$("#regTable").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const email = btn.dataset.email;
  if (!email) return;

  // 行内快捷复制（access/session/refresh 列直接点）
  if (btn.classList.contains("copy-cell")) {
    const field = btn.dataset.field;
    try {
      const cred = await _loadCred(email);
      const val = cred[field] || "";
      if (!val) { alert(`${field} 为空`); return; }
      await _copyText(val, btn);
    } catch (err) { alert("加载凭证失败: " + err.message); }
    return;
  }

  // 「查看凭证」打开模态框
  if (btn.dataset.act === "view") {
    try {
      const cred = await _loadCred(email);
      _renderCredModal(email, cred);
    } catch (err) { alert("加载凭证失败: " + err.message); }
  }

  // 「删除」单行删
  if (btn.dataset.act === "del") {
    if (!confirm(`删除 ${email} 的凭证？`)) return;
    try {
      await api(`/api/registered/${encodeURIComponent(email)}`, { method: "DELETE" });
      refreshRegistered();
    } catch (err) { alert("删除失败: " + err.message); }
  }
});


function _renderCredModal(email, cred) {
  $("#credTitle").textContent = email;
  const box = $("#credFields");
  box.innerHTML = "";

  // 主要凭证按顺序展示，每项独立复制按钮
  const KEYS = [
    ["access_token",  "access_token"],
    ["session_token", "session_token"],
    ["refresh_token", "refresh_token"],
    ["id_token",      "id_token"],
    ["device_id",     "device_id"],
    ["csrf_token",    "csrf_token"],
    ["cookie_header", "cookie_header"],
    ["password",      "password"],
  ];
  for (const [key, label] of KEYS) {
    const val = cred[key] || "";
    if (!val) continue;
    const row = document.createElement("div");
    row.className = "cred-row";
    row.innerHTML = `
      <div class="cred-row-head">
        <span class="cred-label">${label}</span>
        <span class="cred-meta">len=${val.length}</span>
        <button class="cred-copy" data-val-key="${key}">📋 复制</button>
      </div>
      <pre class="cred-val">${escapeHtml(val)}</pre>
    `;
    box.appendChild(row);
  }

  // extra（含 cookie 同步等其他元数据）
  if (cred.extra && Object.keys(cred.extra).length > 0) {
    const row = document.createElement("div");
    row.className = "cred-row";
    row.innerHTML = `
      <div class="cred-row-head">
        <span class="cred-label">extra</span>
        <span class="cred-meta">${Object.keys(cred.extra).length} keys</span>
        <button class="cred-copy" data-val-key="__extra__">📋 复制 JSON</button>
      </div>
      <pre class="cred-val">${escapeHtml(JSON.stringify(cred.extra, null, 2))}</pre>
    `;
    box.appendChild(row);
  }

  $("#credModal").classList.remove("hidden");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// 模态框内单字段复制
$("#credFields").addEventListener("click", async (e) => {
  const btn = e.target.closest("button.cred-copy");
  if (!btn) return;
  const key = btn.dataset.valKey;
  const val = key === "__extra__"
    ? JSON.stringify(_credCache.extra, null, 2)
    : (_credCache[key] || "");
  await _copyText(val, btn);
});

$("#credClose").addEventListener("click", () => {
  $("#credModal").classList.add("hidden");
});
$("#credCopyJson").addEventListener("click", async (e) => {
  if (!_credCache) return;
  await _copyText(JSON.stringify(_credCache, null, 2), e.currentTarget);
});

// ──────────────────────── 运行记录 ────────────────────────

async function refreshRuns() {
  const { items } = await api("/api/runs");
  const tb = $("#runTable tbody");
  tb.innerHTML = "";
  for (const r of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${r.run_id}</code></td>
      <td>${r.email}</td>
      <td><span class="status ${r.status}">${r.status}</span></td>
      <td>${fmtTime(r.started_at)}</td>
      <td title="${r.error || ''}">${(r.error || '').slice(0, 60)}</td>
    `;
    tb.appendChild(tr);
  }
}
$("#btnRefreshRuns").addEventListener("click", refreshRuns);

// ──────────────────────── 🤖 Auto-Loop 全自动批量 ────────────────────────

const AUTO_BTNS = {
  start:  $("#btnAutoStart"),
  pause:  $("#btnAutoPause"),
  resume: $("#btnAutoResume"),
  stop:   $("#btnAutoStop"),
};

function _autoOptions() {
  return {
    proxy: $("#regProxy").value.trim(),
    proxy_pool: $("#autoProxyPool").value,
    concurrency: parseInt($("#autoConcurrency").value || "1", 10),
    otp_timeout: parseInt($("#regOtpTimeout").value || "180", 10),
    want_access_token: true,
    want_session_token: true,
    want_refresh_token: true,
    cool_down_seconds: parseFloat($("#autoCoolDown").value || "3") || 0,
  };
}

async function autoStart() {
  try {
    await api("/api/auto/start", { method: "POST", body: JSON.stringify(_autoOptions()) });
  } catch (e) { alert("启动失败: " + e.message); }
}
async function autoCall(path) {
  try { await api(path, { method: "POST" }); }
  catch (e) { alert(`${path} 失败: ${e.message}`); }
}
AUTO_BTNS.start.addEventListener("click", autoStart);
AUTO_BTNS.pause.addEventListener("click", () => autoCall("/api/auto/pause"));
AUTO_BTNS.resume.addEventListener("click", () => autoCall("/api/auto/resume"));
AUTO_BTNS.stop.addEventListener("click", () => autoCall("/api/auto/stop"));

function _renderAutoStatus(s) {
  const stateLabel = {
    "stopped": "⚪ 未运行",
    "running": "🟢 运行中",
    "paused":  "⏸ 已暂停",
  }[s.state] || s.state;
  const elapsed = s.elapsed ? Math.round(s.elapsed) + "s" : "—";
  const workers = Array.isArray(s.workers) ? s.workers : [];
  const workerRows = workers.length
    ? workers.map(w => {
        const dur = w.started_at ? Math.round(Date.now() / 1000 - w.started_at) + "s" : "";
        const px = w.proxy ? ` [${escapeHtml(w.proxy.slice(0, 30))}${w.proxy.length > 30 ? "..." : ""}]` : "";
        return `<div class="auto-worker">worker-${w.id} ▶ <code>${escapeHtml(w.email)}</code> ${dur}${px}</div>`;
      }).join("")
    : "";
  const meta = `并发=${s.concurrency || 1}` + (s.proxy_pool_size ? ` 代理池=${s.proxy_pool_size}` : "");
  $("#autoStatus").innerHTML = `
    <b>${stateLabel}</b>
    &nbsp;|&nbsp; 已完成: <b class="ok">${s.registered_ok}</b> 成功 / <b class="bad">${s.registered_fail}</b> 失败
    &nbsp;|&nbsp; 运行: ${elapsed}
    &nbsp;|&nbsp; <span class="auto-meta">${meta}</span>
    ${workerRows ? "<br>" + workerRows : ""}
    <br><span class="auto-msg">${escapeHtml(s.last_message || "")}</span>
  `;
  // 按钮可用性
  const st = s.state;
  AUTO_BTNS.start.disabled  = (st === "running" || st === "paused");
  AUTO_BTNS.pause.disabled  = (st !== "running");
  AUTO_BTNS.resume.disabled = (st !== "paused");
  AUTO_BTNS.stop.disabled   = (st === "stopped");
}

let _autoEs = null;
function _connectAutoStream() {
  if (_autoEs) { try { _autoEs.close(); } catch (_) {} }
  const es = new EventSource("/api/auto/stream");
  _autoEs = es;
  es.addEventListener("state", (e) => {
    try { _renderAutoStatus(JSON.parse(e.data)); } catch (_) {}
  });
  es.addEventListener("run_started", (e) => {
    try {
      const d = JSON.parse(e.data);
      logLine(`[auto] ▶ 开始注册 ${d.email} (run=${d.run_id})`, "evt");
      // 复用单跑的 SSE 流，自动接管日志框 + 状态栏复制按钮
      streamRun(d.run_id);
    } catch (_) {}
  });
  es.addEventListener("run_finished", (e) => {
    try {
      const d = JSON.parse(e.data);
      const tag = d.ok ? "✅" : (d.category === "network" ? "🌐 网络错误（号已 release）" : "❌");
      logLine(`[auto] ${tag} ${d.email} 完成`, d.ok ? "ok" : "err");
    } catch (_) {}
  });
  es.addEventListener("circuit_break", (e) => {
    try {
      const d = JSON.parse(e.data);
      logLine(`[auto] ⚠️ 熔断: ${d.reason}`, "err");
      _showBanner(d.reason);
    } catch (_) {}
  });
  es.onerror = () => {
    // 自动重连
    try { es.close(); } catch (_) {}
    _autoEs = null;
    setTimeout(_connectAutoStream, 2000);
  };
}

// 顶部红色告警横幅
function _showBanner(msg) {
  const b = $("#alertBanner");
  $("#alertBannerMsg").textContent = msg;
  b.classList.remove("hidden");
}
$("#alertBannerClose").addEventListener("click", () => {
  $("#alertBanner").classList.add("hidden");
});

// ──────────────────────── 表单持久化（localStorage 自动保存/恢复）────────────────────────

const FORM_KEY = "gpt_outlook_register_form_v1";

// id -> 类型（默认 text；checkbox 走 .checked）
const PERSIST_FIELDS = {
  regProxy:        "text",
  regOtpTimeout:   "text",
  autoCoolDown:    "text",
  autoConcurrency: "text",
  autoProxyPool:   "text",
};

function _saveForm() {
  const data = {};
  for (const [id, kind] of Object.entries(PERSIST_FIELDS)) {
    const el = document.getElementById(id);
    if (!el) continue;
    data[id] = kind === "check" ? !!el.checked : (el.value || "");
  }
  try { localStorage.setItem(FORM_KEY, JSON.stringify(data)); } catch (_) {}
}

function _loadForm() {
  let data = {};
  try { data = JSON.parse(localStorage.getItem(FORM_KEY) || "{}"); } catch (_) { data = {}; }
  for (const [id, kind] of Object.entries(PERSIST_FIELDS)) {
    if (!(id in data)) continue;
    const el = document.getElementById(id);
    if (!el) continue;
    if (kind === "check") el.checked = !!data[id];
    else el.value = data[id] || "";
  }
}

// 绑定 input/change 自动保存
function _bindAutoSave() {
  for (const id of Object.keys(PERSIST_FIELDS)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.addEventListener("input", _saveForm);
    el.addEventListener("change", _saveForm);
  }
}

// ──────────────────────── 📧 邮箱配置 ────────────────────────

function _renderPaymeshStats(stats, count) {
  const s = stats || {};
  const total = count ?? s.total ?? 0;
  $("#paymeshStats").textContent =
    `卡密池：${total} 张，可用 ${s.available || 0} / 使用中 ${s.in_use || 0} / 完成 ${s.done || 0} / 失败 ${s.failed || 0}`;
}

function _renderPaymeshPools(groups = {}) {
  const box = $("#paymeshPools");
  if (!box) return;
  const pools = [
    ["unused", "未使用", groups.unused || []],
    ["used", "已使用", groups.used || []],
    ["failed", "失败", groups.failed || []],
  ];
  box.innerHTML = pools.map(([key, title, items]) => `
    <section class="paymesh-pool ${key}">
      <div class="paymesh-pool-title">${title}<b>${items.length}</b></div>
      <div class="paymesh-pool-list">
        ${items.length ? items.map(item => `
          <div class="paymesh-card" title="${escapeHtml(item.fail_reason || item.email || "")}">
            <code>${escapeHtml(item.card || "")}</code>
            <span class="status ${escapeHtml(item.status || "available")}">${escapeHtml(item.status || "available")}</span>
            <small>${escapeHtml(item.email || item.fail_reason || "-")}</small>
          </div>
        `).join("") : `<div class="paymesh-empty">暂无</div>`}
      </div>
    </section>
  `).join("");
  const btn = $("#btnRetryPaymeshFailed");
  if (btn) btn.disabled = (groups.failed || []).length === 0;
}

async function loadPaymeshCards() {
  const r = await api("/api/paymesh/cards");
  _renderPaymeshStats(r.stats, r.stats?.total || 0);
  _renderPaymeshPools(r.groups || {});
}

function _syncMailSourceFields(source) {
  const isPaymesh = source === "paymesh_card";
  $("#cfTempCfg").classList.toggle("hidden", source !== "cf_temp");
  $("#paymeshCfg").classList.toggle("hidden", !isPaymesh);
  $("#btnTestMail").classList.toggle("hidden", source === "outlook");
  $("#btnTestMail").textContent = isPaymesh
    ? "🔌 测试平台/卡密"
    : "🔌 测试 CF 连通性";
}

async function loadMailConfig() {
  try {
    const { config } = await api("/api/settings/mail");
    const src = config.mail_source || "outlook";
    const radio = document.querySelector(`input[name="mailSource"][value="${src}"]`);
    if (radio) radio.checked = true;
    _syncMailSourceFields(src);
    $("#cfApiUrl").value = config.cf_api_url || "";
    $("#cfDomain").value = config.cf_domain || "";
    $("#cfAdminToken").value = "";
    if (config.cf_admin_token === "***") {
      $("#cfAdminToken").placeholder = "已设置（留空不修改）";
    } else {
      $("#cfAdminToken").placeholder = "Worker 配置的 ADMIN_PASSWORDS";
    }
    $("#paymeshCardCodes").value = config.paymesh_card_codes || "";
    $("#paymeshCardCodes").placeholder = (config.paymesh_card_count || 0) > 0
      ? `已保存 ${config.paymesh_card_count} 张卡密`
      : "XXXX-XXXX-XXXX-XXXX";
    _renderPaymeshStats(config.paymesh_card_stats, config.paymesh_card_count || 0);
    if (src === "paymesh_card") await loadPaymeshCards();
  } catch (e) {
    console.error("loadMailConfig:", e);
  }
}

// radio 切换显隐
document.querySelectorAll("input[name='mailSource']").forEach(r => {
  r.addEventListener("change", () => {
    _syncMailSourceFields(r.value);
    if (r.value === "paymesh_card") loadPaymeshCards().catch(console.error);
  });
});

$("#btnSaveMailCfg").addEventListener("click", async () => {
  const source = document.querySelector("input[name='mailSource']:checked")?.value || "outlook";
  const isCf = source === "cf_temp";
  const isPaymesh = source === "paymesh_card";
  const body = {
    mail_source:    source,
    cf_api_url:     isCf ? $("#cfApiUrl").value.trim() : "",
    cf_admin_token: isCf ? ($("#cfAdminToken").value.trim() || "***") : "***",
    cf_domain:      isCf ? $("#cfDomain").value.trim() : "",
  };
  if (isPaymesh) {
    body.paymesh_card_codes = $("#paymeshCardCodes").value;
  }
  try {
    await api("/api/settings/mail", { method: "POST", body: JSON.stringify(body) });
    $("#mailCfgResult").textContent = "✅ 保存成功";
    $("#mailCfgResult").className = "result ok";
    setTimeout(loadMailConfig, 300);
  } catch (e) {
    $("#mailCfgResult").textContent = "❌ " + e.message;
    $("#mailCfgResult").className = "result bad";
  }
  setTimeout(() => { $("#mailCfgResult").textContent = ""; }, 3000);
});

$("#btnTestMail").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "⏳ 测试中...";
  $("#mailCfgResult").textContent = "";
  try {
    const source = document.querySelector("input[name='mailSource']:checked")?.value || "outlook";
    if (source === "paymesh_card") {
      await api("/api/settings/mail", {
        method: "POST",
        body: JSON.stringify({
          mail_source: source,
          cf_api_url: "",
          cf_admin_token: "***",
          cf_domain: "",
          paymesh_card_codes: $("#paymeshCardCodes").value,
        }),
      });
    }
    const r = await api("/api/settings/mail/test", { method: "POST" });
    $("#mailCfgResult").textContent = "✅ " + r.message;
    $("#mailCfgResult").className = "result ok";
    if (r.stats) {
      _renderPaymeshStats(r.stats, r.stats.total || 0);
      loadPaymeshCards().catch(console.error);
    }
    setTimeout(loadMailConfig, 300);
  } catch (err) {
    $("#mailCfgResult").textContent = "❌ " + err.message;
    $("#mailCfgResult").className = "result bad";
  } finally {
    btn.disabled = false;
    const source = document.querySelector("input[name='mailSource']:checked")?.value || "outlook";
    _syncMailSourceFields(source);
  }
});

$("#btnRetryPaymeshFailed").addEventListener("click", async (e) => {
  if (!confirm("把所有 failed 卡密放回未使用池重试？")) return;
  const btn = e.currentTarget;
  btn.disabled = true;
  $("#paymeshPoolResult").textContent = "处理中...";
  $("#paymeshPoolResult").className = "result";
  try {
    const r = await api("/api/paymesh/cards/retry_failed", { method: "POST" });
    $("#paymeshPoolResult").textContent = `✅ 已放回 ${r.reset} 张 failed 卡密`;
    $("#paymeshPoolResult").className = "result ok";
    await loadPaymeshCards();
  } catch (err) {
    $("#paymeshPoolResult").textContent = "❌ " + err.message;
    $("#paymeshPoolResult").className = "result bad";
  } finally {
    btn.disabled = false;
  }
});

// ──────────────────────── 📱 SMS 接码配置 ────────────────────────

// 全量国家列表（id → name_cn + openai_sms_safe）；首次加载配置时从后端拿
let _smsAllCountries = [];
let _smsSafeCountrySet = new Set();

async function _loadSmsAllCountries() {
  if (_smsAllCountries.length) return _smsAllCountries;
  try {
    const r = await api("/api/settings/sms/all_countries");
    _smsAllCountries = r.countries || [];
    _smsSafeCountrySet = new Set(r.openai_sms_safe || []);
  } catch (e) {
    console.error("加载国家列表失败:", e);
  }
  return _smsAllCountries;
}

function _renderSmsCountrySelect(selectEl, currentValue) {
  selectEl.innerHTML = "";
  for (const c of _smsAllCountries) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = `${c.id} - ${c.name_cn}`;
    selectEl.appendChild(opt);
  }
  if (currentValue) selectEl.value = currentValue;
}

function _renderSmsAllowedCountriesBox(checkedIds) {
  const box = $("#smsAllowedCountriesBox");
  box.innerHTML = "";
  const checkedSet = new Set((checkedIds || "").split(",").map(s => s.trim()).filter(Boolean));

  // 先渲染所有国家（带 data 属性用于搜索）
  for (const c of _smsAllCountries) {
    const lab = document.createElement("label");
    lab.className = "check country-item";
    lab.style.fontSize = "12px";
    lab.style.padding = "4px 6px";
    lab.style.lineHeight = "1.4";
    lab.dataset.countryId = c.id;
    lab.dataset.countryName = c.name_cn.toLowerCase();

    // 显示：ID·国家名 (价格/库存)
    const priceInfo = c.price != null && c.count != null
      ? ` <span style="color:#999;font-size:11px">(${c.price}/${c.count})</span>`
      : "";
    lab.innerHTML = `<input type="checkbox" value="${c.id}" ${checkedSet.has(c.id) ? "checked" : ""}>${c.id}·${c.name_cn}${priceInfo}`;
    box.appendChild(lab);
  }

  _updateAllowedCountryCount();
  box.querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.addEventListener("change", _updateAllowedCountryCount);
  });

  // 绑定搜索框
  const searchInput = $("#smsCountrySearch");
  if (searchInput && !searchInput.dataset.bound) {
    searchInput.dataset.bound = "1";
    searchInput.addEventListener("input", (e) => {
      const query = e.target.value.toLowerCase().trim();
      box.querySelectorAll(".country-item").forEach(lab => {
        const id = lab.dataset.countryId || "";
        const name = lab.dataset.countryName || "";
        const match = !query || id.includes(query) || name.includes(query);
        lab.style.display = match ? "" : "none";
      });
    });
  }
}

function _updateAllowedCountryCount() {
  const checked = $("#smsAllowedCountriesBox").querySelectorAll("input[type=checkbox]:checked");
  $("#smsAllowedCountryCount").textContent = `已选 ${checked.length} 个国家`;
}

function _getAllowedCountriesValue() {
  const checked = $("#smsAllowedCountriesBox").querySelectorAll("input[type=checkbox]:checked");
  return Array.from(checked).map(cb => cb.value).join(",");
}

async function loadSmsConfig() {
  await _loadSmsAllCountries();
  try {
    const { config } = await api("/api/settings/sms");
    $("#smsEnabled").checked = config.sms_enabled === "1";
    const provider = config.sms_provider || "smsbower";
    const radio = document.querySelector(`input[name="smsProvider"][value="${provider}"]`);
    if (radio) radio.checked = true;
    $("#smsApiKey").value = "";
    $("#smsApiKey").placeholder = (config.sms_api_key === "***")
      ? "已设置（留空不修改）"
      : "粘贴接码平台 API Key";
    _renderSmsCountrySelect($("#smsCountry"), config.sms_country || "150");
    $("#smsService").value = config.sms_service || "dr";
    $("#smsMaxPrice").value = config.sms_max_price || "";
    $("#smsPhoneSuccessMax").value = config.sms_phone_success_max || "3";
    $("#smsReusePhone").checked = config.sms_reuse_phone === "1";
    $("#smsAutoCountry").checked = config.sms_auto_country === "1";
    $("#smsAutoMinStock").value = config.sms_auto_min_stock || "20";
    $("#smsAutoMaxPrice").value = config.sms_auto_max_price || "";
    _renderSmsAllowedCountriesBox(config.sms_allowed_countries || "");
    $("#smsMaxPhoneAttempts").value = config.sms_max_phone_attempts || "";
    $("#smsPerPhoneTimeout").value = config.sms_per_phone_timeout || "80";
  } catch (e) {
    console.error("loadSmsConfig:", e);
  }
}

$("#btnClearAllowedCountries")?.addEventListener("click", () => {
  $("#smsAllowedCountriesBox").querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.checked = false;
  });
  _updateAllowedCountryCount();
});

function _smsConfigFormBody() {
  const apiKeyInput = $("#smsApiKey").value.trim();
  return {
    sms_enabled:           $("#smsEnabled").checked ? "1" : "0",
    sms_provider:          document.querySelector("input[name='smsProvider']:checked")?.value || "smsbower",
    sms_api_key:           apiKeyInput || "***",
    sms_country:           $("#smsCountry").value.trim() || "52",
    sms_service:           $("#smsService").value.trim() || "dr",
    sms_max_price:         $("#smsMaxPrice").value.trim(),
    sms_phone_success_max: $("#smsPhoneSuccessMax").value.trim() || "3",
    sms_reuse_phone:       $("#smsReusePhone").checked ? "1" : "0",
    sms_auto_country:      $("#smsAutoCountry").checked ? "1" : "0",
    sms_allowed_countries: _getAllowedCountriesValue(),
    sms_auto_min_stock:    $("#smsAutoMinStock").value.trim() || "20",
    sms_auto_max_price:    $("#smsAutoMaxPrice").value.trim(),
    sms_max_phone_attempts: $("#smsMaxPhoneAttempts").value.trim(),
    sms_per_phone_timeout: $("#smsPerPhoneTimeout").value.trim() || "80",
  };
}

$("#btnSaveSmsCfg").addEventListener("click", async () => {
  const body = _smsConfigFormBody();
  try {
    await api("/api/settings/sms", { method: "POST", body: JSON.stringify(body) });
    $("#smsCfgResult").textContent = "✅ 保存成功";
    $("#smsCfgResult").className = "result ok";
    setTimeout(loadSmsConfig, 300);
  } catch (e) {
    $("#smsCfgResult").textContent = "❌ " + e.message;
    $("#smsCfgResult").className = "result bad";
  }
  setTimeout(() => { $("#smsCfgResult").textContent = ""; }, 3500);
});

$("#btnTestSms").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "⏳ 测试中...";
  $("#smsCfgResult").textContent = "";
  try {
    const r = await api("/api/settings/sms/test", {
      method: "POST",
      body: JSON.stringify(_smsConfigFormBody()),
    });
    $("#smsCfgResult").textContent = "✅ " + r.message;
    $("#smsCfgResult").className = "result ok";
  } catch (err) {
    $("#smsCfgResult").textContent = "❌ " + err.message;
    $("#smsCfgResult").className = "result bad";
  } finally {
    btn.disabled = false;
    btn.textContent = "🔌 测试余额";
  }
});

// ──────────────────────── 📤 自动导出配置 (CPA / SUB2API) ────────────────────────

async function loadExportConfig() {
  try {
    const { config } = await api("/api/settings/export");
    // CPA
    $("#cpaEnabled").checked = config.cpa_enabled === "1";
    $("#cpaUrl").value = config.cpa_url || "";
    $("#cpaMgmtKey").value = "";
    $("#cpaMgmtKey").placeholder = config.cpa_mgmt_key === "***"
      ? "已设置（留空不修改）"
      : "粘贴 CPA 管理密钥";
    $("#cpaTimeout").value = config.cpa_timeout || "30";
    // SUB2API
    $("#sub2apiEnabled").checked = config.sub2api_enabled === "1";
    $("#sub2apiUrl").value = config.sub2api_url || "";
    $("#sub2apiApiKey").value = "";
    $("#sub2apiApiKey").placeholder = config.sub2api_api_key === "***"
      ? "已设置（留空不修改）"
      : "粘贴面板里生成的 x-api-key";
    $("#sub2apiGroupIds").value = config.sub2api_group_ids || "2";
    $("#sub2apiProxyId").value = config.sub2api_proxy_id || "";
    $("#sub2apiTimeout").value = config.sub2api_timeout || "30";

    // K12 Workspace
    await loadK12Config();
  } catch (e) {
    console.error("loadExportConfig:", e);
  }
}

async function loadK12Config() {
  try {
    const { config } = await api("/api/settings/k12");
    $("#k12Enabled").checked = config.k12_enabled === "1";
    $("#k12WorkspaceIds").value = config.k12_workspace_ids || "";
  } catch (e) {
    console.error("loadK12Config:", e);
  }
}

$("#btnSaveExportCfg").addEventListener("click", async () => {
  const cpaKeyInput = $("#cpaMgmtKey").value.trim();
  const sub2apiKeyInput = $("#sub2apiApiKey").value.trim();
  const body = {
    // CPA
    cpa_enabled:  $("#cpaEnabled").checked ? "1" : "0",
    cpa_url:      $("#cpaUrl").value.trim(),
    cpa_mgmt_key: cpaKeyInput || "***",
    cpa_timeout:  $("#cpaTimeout").value.trim() || "30",
    // SUB2API
    sub2api_enabled:    $("#sub2apiEnabled").checked ? "1" : "0",
    sub2api_url:        $("#sub2apiUrl").value.trim(),
    sub2api_api_key:    sub2apiKeyInput || "***",
    sub2api_group_ids:  $("#sub2apiGroupIds").value.trim() || "2",
    sub2api_proxy_id:   $("#sub2apiProxyId").value.trim(),
    sub2api_timeout:    $("#sub2apiTimeout").value.trim() || "30",
  };
  try {
    await api("/api/settings/export", { method: "POST", body: JSON.stringify(body) });

    // K12 配置单独保存
    const k12Body = {
      k12_enabled: $("#k12Enabled").checked ? "1" : "0",
      k12_workspace_ids: $("#k12WorkspaceIds").value.trim(),
    };
    await api("/api/settings/k12", { method: "POST", body: JSON.stringify(k12Body) });

    $("#exportCfgResult").textContent = "✅ 保存成功";
    $("#exportCfgResult").className = "result ok";
    setTimeout(loadExportConfig, 300);
  } catch (e) {
    $("#exportCfgResult").textContent = "❌ " + e.message;
    $("#exportCfgResult").className = "result bad";
  }
  setTimeout(() => { $("#exportCfgResult").textContent = ""; }, 3500);
});

async function _testExportTarget(target, btn, resultEl, origText) {
  btn.disabled = true;
  btn.textContent = "⏳ 测试中...";
  resultEl.textContent = "";
  try {
    const r = await api("/api/settings/export/test", {
      method: "POST",
      body: JSON.stringify({ target }),
    });
    resultEl.textContent = "✅ " + (r.message || "连通正常");
    resultEl.className = "result ok";
  } catch (e) {
    resultEl.textContent = "❌ " + e.message;
    resultEl.className = "result bad";
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

$("#btnTestCpa").addEventListener("click", (e) => {
  _testExportTarget("cpa", e.currentTarget, $("#cpaTestResult"), "🔌 测试 CPA 连通性");
});
$("#btnTestSub2api").addEventListener("click", (e) => {
  _testExportTarget("sub2api", e.currentTarget, $("#sub2apiTestResult"), "🔌 测试 SUB2API 连通性");
});

async function runSub2apiAccountCheck(e, dryRun, selectedOnly = false) {
  if (currentEs) {
    alert("Another run is still streaming. Wait for it to finish first.");
    return;
  }
  const btn = e.currentTarget;
  const selectedIds = selectedOnly ? selectedSub2apiAccountIds() : [];
  if (selectedOnly && !selectedIds.length) return;
  const body = {
    scan_limit: parseInt($("#sub2apiReauthScanLimit").value || "1000", 10),
    max_accounts: parseInt($("#sub2apiReauthMax").value || "5", 10),
    probe_usage: $("#sub2apiProbeUsage").checked,
    proxy: $("#regProxy").value.trim(),
    otp_timeout: parseInt($("#regOtpTimeout").value || "180", 10),
    dry_run: !!dryRun,
  };
  if (selectedIds.length) body.account_ids = selectedIds;
  btn.disabled = true;
  if (!selectedOnly) resetSub2apiScanResults();
  $("#sub2apiReauthResult").textContent = dryRun
    ? "checking..."
    : (selectedIds.length ? `starting selected ${selectedIds.length}...` : "starting...");
  $("#sub2apiReauthResult").className = "result";
  $("#logBox").innerHTML = "";
  try {
    const r = await api("/api/sub2api/reauth_401", {
      method: "POST",
      body: JSON.stringify(body),
    });
    const msg = dryRun
      ? `SUB2API account check started: run_id=${r.run_id}`
      : `SUB2API reauth started: run_id=${r.run_id}${selectedIds.length ? ` selected=${selectedIds.length}` : ""}`;
    $("#sub2apiReauthResult").textContent = msg;
    $("#sub2apiReauthResult").className = "result ok";
    $("#runStatus").textContent = msg;
    logLine("[client] " + msg, "evt");
    streamRun(r.run_id);
  } catch (err) {
    $("#sub2apiReauthResult").textContent = "failed: " + err.message;
    $("#sub2apiReauthResult").className = "result bad";
  } finally {
    btn.disabled = false;
    if (selectedOnly) updateSub2apiDeleteButton();
  }
}

$("#btnSub2apiCheckAccounts").addEventListener("click", (e) => runSub2apiAccountCheck(e, true));
$("#btnSub2apiReauth401").addEventListener("click", (e) => runSub2apiAccountCheck(e, false));

$("#btnTestK12").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const resultEl = $("#k12TestResult");
  btn.disabled = true;
  btn.textContent = "⏳ 测试中...";
  resultEl.textContent = "";
  try {
    const r = await api("/api/settings/k12/test", { method: "POST" });
    let msg = "✅ " + (r.message || "格式验证通过");
    const last = r.last_result || {};
    const rows = Array.isArray(last.workspace_results) ? last.workspace_results : [];
    if (rows.length) {
      const ok = rows.filter(x => x.ok).map(x => x.workspace_id);
      const bad = rows.filter(x => !x.ok && x.status !== "skipped");
      const skipped = rows.filter(x => x.status === "skipped").map(x => x.workspace_id);
      const show = xs => xs.slice(0, 8).map(x => (typeof x === "string" ? x : x.workspace_id).slice(0, 8)).join(", ");
      msg += `；最近实测：可用 ${ok.length}，不可用 ${bad.length}，跳过 ${skipped.length}`;
      if (ok.length) msg += `；可用: ${show(ok)}`;
      if (bad.length) msg += `；不可用: ${show(bad)}`;
      resultEl.title = JSON.stringify(rows, null, 2);
    } else {
      msg += "；暂无真实 join 结果，跑一次注册后这里会显示哪些能用";
      resultEl.title = "";
    }
    resultEl.textContent = msg;
    resultEl.className = "result ok";
  } catch (e) {
    resultEl.textContent = "❌ " + e.message;
    resultEl.className = "result bad";
  } finally {
    btn.disabled = false;
    btn.textContent = "🔌 测试 Workspace ID 格式";
  }
});

// ──────────────────────── 启动 ────────────────────────

_loadForm();
_bindAutoSave();
refreshStats();
refreshPool();
_connectAutoStream();
setInterval(refreshStats, 5000);
