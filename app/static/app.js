/**
 * BVC Logistics & TallyPrime Middleware - Frontend SPA
 * Vanilla JavaScript implementation (No build step)
 */

document.addEventListener("DOMContentLoaded", () => {
  // Global State
  let allVouchers = [];
  let selectedGuids = new Set();
  let appConfig = null;

  // DOM Elements
  const fromDateInput = document.getElementById("from-date");
  const toDateInput = document.getElementById("to-date");
  const presetButtons = document.querySelectorAll(".preset-btn");
  const voucherTypeSelect = document.getElementById("voucher-type");
  const statusFilterSelect = document.getElementById("status-filter");
  const searchInput = document.getElementById("search-input");
  const fetchBtn = document.getElementById("fetch-btn");
  const headerCheckbox = document.getElementById("header-checkbox");
  const selectAllBtn = document.getElementById("select-all-btn");
  const createShipmentsBtn = document.getElementById("create-shipments-btn");
  const vouchersTbody = document.getElementById("vouchers-tbody");
  const alertBox = document.getElementById("alert-box");
  const alertMessage = document.getElementById("alert-message");
  const progressContainer = document.getElementById("progress-container");
  const progressBarFill = document.getElementById("progress-bar-fill");
  const progressCounter = document.getElementById("progress-counter");
  const progressStatusMsg = document.getElementById("progress-status-msg");

  // Stat elements
  const statTotal = document.getElementById("stat-total");
  const statReady = document.getElementById("stat-ready");
  const statNotReady = document.getElementById("stat-not-ready");
  const statShipped = document.getElementById("stat-shipped");
  const statFailed = document.getElementById("stat-failed");
  const statSelected = document.getElementById("stat-selected");
  const btnSelectedCount = document.getElementById("btn-selected-count");

  // --------------------------------------------------------------------------
  // 1. Date Calculation & Presets
  // --------------------------------------------------------------------------
  function formatISODate(d) {
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function applyPreset(preset) {
    const now = new Date();
    let fromDate, toDate;

    if (preset === "today") {
      fromDate = formatISODate(now);
      toDate = formatISODate(now);
    } else if (preset === "yesterday") {
      const yesterday = new Date(now);
      yesterday.setDate(now.getDate() - 1);
      fromDate = formatISODate(yesterday);
      toDate = formatISODate(yesterday);
    } else if (preset === "week") {
      const day = now.getDay();
      const diffToMonday = now.getDate() - (day === 0 ? 6 : day - 1);
      const monday = new Date(now.setDate(diffToMonday));
      const sunday = new Date(monday);
      sunday.setDate(monday.getDate() + 6);
      fromDate = formatISODate(monday);
      toDate = formatISODate(sunday);
    } else if (preset === "month") {
      const firstDay = new Date(now.getFullYear(), now.getMonth(), 1);
      const lastDay = new Date(now.getFullYear(), now.getMonth() + 1, 0);
      fromDate = formatISODate(firstDay);
      toDate = formatISODate(lastDay);
    } else if (preset === "last_month") {
      const firstDay = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      const lastDay = new Date(now.getFullYear(), now.getMonth(), 0);
      fromDate = formatISODate(firstDay);
      toDate = formatISODate(lastDay);
    }

    fromDateInput.value = fromDate;
    toDateInput.value = toDate;

    presetButtons.forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.preset === preset);
    });
  }

  // --------------------------------------------------------------------------
  // 2. Alert & Notifications
  // --------------------------------------------------------------------------
  function showAlert(msg, isError = true) {
    alertMessage.textContent = msg;
    alertBox.classList.remove("d-none");
    alertBox.style.backgroundColor = isError ? "rgba(239, 68, 68, 0.15)" : "rgba(16, 185, 129, 0.15)";
    alertBox.style.borderColor = isError ? "rgba(239, 68, 68, 0.4)" : "rgba(16, 185, 129, 0.4)";
    alertBox.style.color = isError ? "#fee2e2" : "#dcfce7";
  }

  function hideAlert() {
    alertBox.classList.add("d-none");
  }

  // --------------------------------------------------------------------------
  // 3. System Config & Health Polling
  // --------------------------------------------------------------------------
  async function loadConfig() {
    try {
      const resp = await fetch("/api/config");
      if (!resp.ok) return;
      appConfig = await resp.json();

      const envBadge = document.getElementById("env-badge");
      if (appConfig.is_production) {
        envBadge.textContent = "PRODUCTION";
        envBadge.className = "badge badge-prod";
      } else {
        envBadge.textContent = "UAT / STAGING";
        envBadge.className = "badge badge-uat";
      }
    } catch (e) {
      console.warn("Could not load /api/config:", e);
    }
  }

  async function checkHealth() {
    const tallyDot = document.getElementById("tally-dot");
    const tallyText = document.getElementById("tally-status-text");
    const bvcDot = document.getElementById("bvc-dot");
    const bvcText = document.getElementById("bvc-status-text");

    try {
      const resp = await fetch("/health");
      const data = await resp.json();

      if (data.tally && data.tally.online) {
        tallyDot.className = "dot dot-green";
        tallyText.textContent = "Tally: Online (Port 9000)";
      } else {
        tallyDot.className = "dot dot-red";
        tallyText.textContent = "Tally: Offline";
      }

      if (data.bvc && data.bvc.reachable) {
        bvcDot.className = "dot dot-green";
        bvcText.textContent = "BVC: Reachable";
      } else {
        bvcDot.className = "dot dot-red";
        bvcText.textContent = "BVC: Unreachable";
      }
    } catch (e) {
      tallyDot.className = "dot dot-red";
      tallyText.textContent = "Tally: Error";
      bvcDot.className = "dot dot-red";
      bvcText.textContent = "BVC: Error";
    }
  }

  // --------------------------------------------------------------------------
  // 4. Fetch Vouchers from Tally
  // --------------------------------------------------------------------------
  async function fetchVouchers() {
    hideAlert();
    const fromDate = fromDateInput.value;
    const toDate = toDateInput.value;
    const vType = voucherTypeSelect.value;

    if (!fromDate || !toDate) {
      showAlert("Please choose both From and To dates.");
      return;
    }

    fetchBtn.disabled = true;
    fetchBtn.innerHTML = '<span class="btn-icon">⏳</span> Fetching...';
    vouchersTbody.innerHTML = `<tr><td colspan="13" class="empty-state"><div class="empty-icon">⏳</div><div class="empty-title">Contacting TallyPrime...</div><div class="empty-desc">Exporting Sales Vouchers from ${fromDate} to ${toDate}</div></td></tr>`;

    try {
      const url = `/api/vouchers?from=${encodeURIComponent(fromDate)}&to=${encodeURIComponent(toDate)}&voucher_type=${encodeURIComponent(vType)}`;
      const resp = await fetch(url);
      const data = await resp.json();

      if (!resp.ok || !data.ok) {
        const errMsg = data.error || "Failed to fetch vouchers from TallyPrime.";
        showAlert(`Tally Error: ${errMsg}`, true);
        vouchersTbody.innerHTML = `<tr><td colspan="13" class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-title">Tally Connection Error</div><div class="empty-desc">${errMsg}</div></td></tr>`;
        allVouchers = [];
        updateSummary();
        return;
      }

      allVouchers = data.vouchers || [];
      selectedGuids.clear();
      renderVouchersTable();
    } catch (e) {
      showAlert(`Network communication error: ${e.message}`, true);
      vouchersTbody.innerHTML = `<tr><td colspan="13" class="empty-state"><div class="empty-icon">❌</div><div class="empty-title">Network Error</div><div class="empty-desc">${e.message}</div></td></tr>`;
      allVouchers = [];
      updateSummary();
    } finally {
      fetchBtn.disabled = false;
      fetchBtn.innerHTML = '<span class="btn-icon">🔄</span> Fetch from Tally';
    }
  }

  // --------------------------------------------------------------------------
  // 5. Filter & Render Table
  // --------------------------------------------------------------------------
  function getFilteredVouchers() {
    const statusFilter = statusFilterSelect.value;
    const searchTerm = searchInput.value.toLowerCase().trim();

    return allVouchers.filter((v) => {
      // Status filter
      if (statusFilter === "ready" && !v.is_ready) return false;
      if (statusFilter === "not_ready" && (v.is_ready || v.is_shipped)) return false;
      if (statusFilter === "shipped" && !v.is_shipped) return false;
      if (statusFilter === "failed" && v.status !== "Failed") return false;

      // Search term
      if (searchTerm) {
        const vno = (v.voucher_number || "").toLowerCase();
        const party = (v.party_name || "").toLowerCase();
        if (!vno.includes(searchTerm) && !party.includes(searchTerm)) {
          return false;
        }
      }

      return true;
    });
  }

  function renderVouchersTable() {
    const filtered = getFilteredVouchers();
    updateSummary();

    if (filtered.length === 0) {
      vouchersTbody.innerHTML = `
        <tr>
          <td colspan="13" class="empty-state">
            <div class="empty-icon">🔍</div>
            <div class="empty-title">No Matching Vouchers</div>
            <div class="empty-desc">No vouchers match the active filters or search criteria.</div>
          </td>
        </tr>`;
      headerCheckbox.checked = false;
      return;
    }

    let html = "";
    filtered.forEach((v) => {
      const isSelected = selectedGuids.has(v.guid);
      const isRowDisabled = !v.is_ready || v.is_shipped;

      // Validation Badge
      let valHtml = "";
      if (v.is_shipped) {
        valHtml = '<span class="val-ready" style="background:rgba(56,189,248,0.15);color:#38bdf8;border-color:rgba(56,189,248,0.4)">Shipped</span>';
      } else if (v.is_ready) {
        if (v.warnings && v.warnings.length > 0) {
          const warnTip = v.warnings.join("\n");
          valHtml = `<span class="val-ready" title="${escapeHtml(warnTip)}" style="background:rgba(234,179,8,0.15);color:#eab308;border-color:rgba(234,179,8,0.4);cursor:help">Ready ⚠️</span>`;
        } else {
          valHtml = '<span class="val-ready">Ready</span>';
        }
      } else {
        const allNotes = [...(v.missing_fields || []), ...(v.warnings ? v.warnings.map(w => `[Warning] ${w}`) : [])];
        const tooltip = allNotes.join("\n");
        valHtml = `<span class="val-missing" title="${escapeHtml(tooltip)}">Missing (${v.missing_fields.length})</span>`;
      }

      // Status Badge
      let statusBadgeClass = "status-pending";
      if (v.status === "Created") statusBadgeClass = "status-created";
      else if (v.status === "Failed") statusBadgeClass = "status-failed";

      const statusHtml = `<span class="status-badge ${statusBadgeClass}">${escapeHtml(v.status || "Pending")}</span>`;
      const snHtml = v.shipment_no ? `<strong class="font-mono" style="color:#38bdf8">${escapeHtml(v.shipment_no)}</strong>` : "-";

      html += `
        <tr class="${isRowDisabled ? "row-disabled" : ""}" data-guid="${escapeHtml(v.guid)}">
          <td>
            <input type="checkbox" class="row-checkbox" value="${escapeHtml(v.guid)}" ${isSelected ? "checked" : ""} ${isRowDisabled ? "disabled" : ""}>
          </td>
          <td>${escapeHtml(v.date || "-")}</td>
          <td><strong class="font-mono">${escapeHtml(v.voucher_number || "-")}</strong></td>
          <td>${escapeHtml(v.party_name || "-")}</td>
          <td>${escapeHtml(v.pincode || "-")}</td>
          <td><span class="badge" style="background:#1e293b;border:1px solid #475569;font-size:0.7rem">${escapeHtml(v.service || "-")}</span></td>
          <td>${escapeHtml(v.product || "-")}</td>
          <td class="text-right">${escapeHtml(v.net_wt ? v.net_wt + "g" : "-")}</td>
          <td class="text-right">${escapeHtml(v.gross_wt ? v.gross_wt + "g" : "-")}</td>
          <td class="text-right font-mono">${escapeHtml(v.amount ? "₹" + Number(v.amount).toLocaleString("en-IN") : "-")}</td>
          <td>${valHtml}</td>
          <td>${snHtml}</td>
          <td>${statusHtml}</td>
        </tr>`;
    });

    vouchersTbody.innerHTML = html;

    // Attach checkbox listeners
    document.querySelectorAll(".row-checkbox").forEach((cb) => {
      cb.addEventListener("change", (e) => {
        const guid = e.target.value;
        if (e.target.checked) selectedGuids.add(guid);
        else selectedGuids.delete(guid);
        updateSummary();
      });
    });

    // Check header checkbox if all enabled filtered are selected
    const selectable = filtered.filter((v) => v.is_ready && !v.is_shipped);
    headerCheckbox.checked = selectable.length > 0 && selectable.every((v) => selectedGuids.has(v.guid));
  }

  function updateSummary() {
    const total = allVouchers.length;
    const ready = allVouchers.filter((v) => v.is_ready).length;
    const notReady = allVouchers.filter((v) => !v.is_ready && !v.is_shipped).length;
    const shipped = allVouchers.filter((v) => v.is_shipped).length;
    const failed = allVouchers.filter((v) => v.status === "Failed").length;
    const selected = selectedGuids.size;

    statTotal.textContent = total;
    statReady.textContent = ready;
    statNotReady.textContent = notReady;
    statShipped.textContent = shipped;
    statFailed.textContent = failed;
    statSelected.textContent = selected;
    btnSelectedCount.textContent = selected;

    createShipmentsBtn.disabled = selected === 0;
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // --------------------------------------------------------------------------
  // 6. Selection Actions
  // --------------------------------------------------------------------------
  headerCheckbox.addEventListener("change", (e) => {
    const isChecked = e.target.checked;
    const filtered = getFilteredVouchers();
    filtered.forEach((v) => {
      if (v.is_ready && !v.is_shipped) {
        if (isChecked) selectedGuids.add(v.guid);
        else selectedGuids.delete(v.guid);
      }
    });
    renderVouchersTable();
  });

  selectAllBtn.addEventListener("click", () => {
    allVouchers.forEach((v) => {
      if (v.is_ready && !v.is_shipped) {
        selectedGuids.add(v.guid);
      }
    });
    renderVouchersTable();
  });

  // --------------------------------------------------------------------------
  // 7. Create BVC Shipments (Batch Execution)
  // --------------------------------------------------------------------------
  createShipmentsBtn.addEventListener("click", async () => {
    const guidsToProcess = Array.from(selectedGuids);
    if (guidsToProcess.length === 0) return;

    if (!confirm(`Are you sure you want to push ${guidsToProcess.length} Sales Voucher(s) to BVC Logistics?`)) {
      return;
    }

    createShipmentsBtn.disabled = true;
    fetchBtn.disabled = true;
    progressContainer.classList.remove("d-none");
    progressBarFill.style.width = "0%";
    progressCounter.textContent = `0 / ${guidsToProcess.length}`;
    progressStatusMsg.textContent = "Connecting to BVC Logistics API...";

    try {
      const resp = await fetch("/api/shipments/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ guids: guidsToProcess }),
      });
      const data = await resp.json();

      progressBarFill.style.width = "100%";
      progressCounter.textContent = `${data.total} / ${data.total}`;

      if (data.results) {
        // Update local voucher rows
        data.results.forEach((r) => {
          const v = allVouchers.find((item) => item.guid === r.guid);
          if (v) {
            v.status = r.status;
            if (r.shipping_note_no) {
              v.shipment_no = r.shipping_note_no;
              v.is_shipped = true;
              v.is_ready = false;
            }
            if (r.message && !r.ok) {
              v.error = r.message;
            }
          }
        });
      }

      selectedGuids.clear();
      renderVouchersTable();

      if (data.ok) {
        progressStatusMsg.innerHTML = `<span style="color:#10b981">✅ Batch Complete: ${data.success_count} shipment(s) created successfully!</span>`;
        showAlert(`Successfully created ${data.success_count} shipment(s).`, false);
      } else {
        progressStatusMsg.innerHTML = `<span style="color:#ef4444">⚠️ Batch Complete: ${data.success_count} succeeded, ${data.fail_count} failed. Check row errors.</span>`;
        showAlert(`Batch completed with errors: ${data.fail_count} failed.`, true);
      }
    } catch (e) {
      progressStatusMsg.innerHTML = `<span style="color:#ef4444">❌ Network failure during batch execution: ${e.message}</span>`;
      showAlert(`Error: ${e.message}`, true);
    } finally {
      createShipmentsBtn.disabled = false;
      fetchBtn.disabled = false;
    }
  });

  // --------------------------------------------------------------------------
  // 8. Event Listeners & Init
  // --------------------------------------------------------------------------
  presetButtons.forEach((btn) => {
    btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
  });

  fetchBtn.addEventListener("click", fetchVouchers);
  statusFilterSelect.addEventListener("change", renderVouchersTable);
  searchInput.addEventListener("input", renderVouchersTable);

  // Initialize
  applyPreset("month"); // Default to This Month
  loadConfig();
  checkHealth();

  // Poll health every 30 seconds
  setInterval(checkHealth, 30000);
});

// Modal helpers
function closeModal() {
  document.getElementById("detail-modal").classList.add("d-none");
}
