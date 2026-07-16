document.addEventListener("DOMContentLoaded", function () {
  var pagination = document.getElementById("batch-pagination");
  var currentPage = parseInt(pagination ? pagination.dataset.page : "1");
  var totalPages = parseInt(pagination ? pagination.dataset.pages : "1");
  var batchVersion = pagination ? (pagination.dataset.version || "") : "";
  var searchInput = document.getElementById("search-input");
  var searchTimer = null;
  var refreshIntervalInput = document.getElementById("refresh-interval");
  var refreshCountdown = document.getElementById("refresh-countdown");
  var refreshTimeout = null;
  var countdownTimer = null;
  var nextRefreshAt = 0;
  var savedRefreshSeconds = parseInt(localStorage.getItem("batchRefreshSeconds") || "30", 10);
  var refreshSeconds = Math.max(3, Math.min(300, isNaN(savedRefreshSeconds) ? 30 : savedRefreshSeconds));
  if (refreshIntervalInput) refreshIntervalInput.value = refreshSeconds;

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function renderRows(items) {
    var tbody = document.getElementById("batch-rows");
    if (!tbody) return;
    if (!items || items.length === 0) {
      tbody.innerHTML = '<tr><td class="py-8 text-center text-zinc-400 px-2.5" colspan="5">暂无邮箱</td></tr>';
      return;
    }
    var html = "";
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      html += '<tr class="border-b border-zinc-100" data-email="' + esc(it.email) + '">';
      if (it.latest) {
        html += '<td class="cell-subject truncate max-w-0 font-medium text-sky-700 underline cursor-pointer hover:text-sky-900 px-2.5 py-1.5" data-id="' + it.latest.id + '">' + esc(it.latest.subject || "(无主题)") + '</td>';
        html += '<td class="cell-sender truncate text-zinc-700 px-2.5 py-1.5">' + esc(it.latest.sender_email || "-") + '</td>';
        html += '<td class="cell-recipient truncate text-zinc-700 px-2.5 py-1.5">' + esc(it.email) + '</td>';
        html += '<td class="cell-time whitespace-nowrap text-xs text-zinc-500 px-2.5 py-1.5">' + esc(it.latest.time_text) + '</td>';
        html += '<td class="cell-code px-2.5 py-1.5">';
        if (it.latest.code) {
          html += '<a class="font-mono text-sm font-semibold text-sky-600 underline hover:text-sky-800" href="#" data-code="' + esc(it.latest.code) + '">' + esc(it.latest.code) + '</a>';
        } else {
          html += '<span class="font-mono text-sm text-zinc-400">\u2014</span>';
        }
        html += '</td>';
      } else {
        html += '<td class="cell-subject text-zinc-400 px-2.5 py-1.5" data-id="0">暂无邮件</td>';
        html += '<td class="cell-sender text-zinc-400 px-2.5 py-1.5">\u2014</td>';
        html += '<td class="cell-recipient truncate text-zinc-700 px-2.5 py-1.5">' + esc(it.email) + '</td>';
        html += '<td class="cell-time text-zinc-400 px-2.5 py-1.5">\u2014</td>';
        html += '<td class="cell-code px-2.5 py-1.5"><span class="font-mono text-sm text-zinc-400">\u2014</span></td>';
      }
      html += '</tr>';
    }
    tbody.innerHTML = html;
  }

  function updatePagination(data) {
    currentPage = data.page;
    totalPages = data.pages;
    if (data.version != null) batchVersion = data.version;
    if (pagination && data.version != null) pagination.dataset.version = data.version;
    var total = data.total;
    var info = document.getElementById("page-info");
    if (info) {
      if (total > 0) {
        var start = (currentPage - 1) * 20 + 1;
        var end = Math.min(currentPage * 20, total);
        info.textContent = "\u7B2C " + start + "-" + end + " \u6761\uFF0C\u5171 " + total + " \u6761";
      } else {
        info.textContent = "\u7B2C 0-0 \u6761\uFF0C\u5171 0 \u6761";
      }
    }
    var prevBtn = document.getElementById("prev-page");
    if (prevBtn) prevBtn.disabled = currentPage <= 1;
    var nextBtn = document.getElementById("next-page");
    if (nextBtn) nextBtn.disabled = currentPage >= totalPages;
  }

  function fetchData(page, q, cb, useVersion) {
    var params = "?page=" + page;
    if (q) params += "&q=" + encodeURIComponent(q);
    if (useVersion && !q && batchVersion) params += "&version=" + encodeURIComponent(batchVersion);
    fetch("/admin/batch/data" + params, { headers: { "X-Requested-With": "fetch" } })
      .then(function (res) { return res.json(); })
      .then(cb)
      .catch(function () {});
  }

  function setDialogBody(html) {
    var body = document.getElementById("dialog-body");
    if (!body) return;
    var oldFrame = document.getElementById("dialog-iframe");
    var iframe = document.createElement("iframe");
    iframe.id = "dialog-iframe";
    iframe.className = "w-full";
    iframe.setAttribute("sandbox", "");
    iframe.style.height = "65vh";
    iframe.style.border = "none";
    iframe.style.display = "block";
    iframe.srcdoc = html;
    if (oldFrame) {
      body.replaceChild(iframe, oldFrame);
    } else {
      body.innerHTML = "";
      body.appendChild(iframe);
    }
  }

  function showMessageDialog() {
    var dialog = document.getElementById("message-dialog");
    dialog.classList.remove("closing");
    if (dialog.open) return;
    if (typeof openDialog === "function") openDialog(dialog);
    else if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function refresh() {
    if (document.hidden) return;
    var q = searchInput ? searchInput.value : "";
    fetchData(currentPage, q, function (data) {
      if (data.unchanged) {
        updatePagination(data);
        return;
      }
      renderRows(data.items);
      updatePagination(data);
    }, true);
  }

  function updateCountdown() {
    if (!refreshCountdown) return;
    if (document.hidden) {
      refreshCountdown.textContent = "已暂停";
      return;
    }
    var remaining = Math.max(0, Math.ceil((nextRefreshAt - Date.now()) / 1000));
    refreshCountdown.textContent = remaining + " 秒后刷新";
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimeout);
    nextRefreshAt = Date.now() + refreshSeconds * 1000;
    updateCountdown();
    refreshTimeout = setTimeout(function () {
      refresh();
      scheduleRefresh();
    }, refreshSeconds * 1000);
  }

  function goPage(page) {
    if (page < 1 || page > totalPages) return;
    var q = searchInput ? searchInput.value : "";
    fetchData(page, q, function (data) {
      currentPage = data.page;
      totalPages = data.pages;
      renderRows(data.items);
      updatePagination(data);
    }, false);
  }

  function doSearch() {
    currentPage = 1;
    var q = searchInput ? searchInput.value : "";
    fetchData(1, q, function (data) {
      renderRows(data.items);
      updatePagination(data);
    }, false);
  }

  refresh();
  scheduleRefresh();
  countdownTimer = setInterval(updateCountdown, 250);

  var refreshBtn = document.getElementById("refresh-btn");
  if (refreshBtn) refreshBtn.addEventListener("click", function () {
    refresh();
    scheduleRefresh();
  });

  if (refreshIntervalInput) {
    refreshIntervalInput.addEventListener("change", function () {
      var value = parseInt(refreshIntervalInput.value, 10);
      refreshSeconds = Math.max(3, Math.min(300, isNaN(value) ? 30 : value));
      refreshIntervalInput.value = refreshSeconds;
      localStorage.setItem("batchRefreshSeconds", String(refreshSeconds));
      scheduleRefresh();
      if (window.showToast) showToast("自动刷新间隔已设为 " + refreshSeconds + " 秒");
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      clearTimeout(refreshTimeout);
      updateCountdown();
    } else {
      refresh();
      scheduleRefresh();
    }
  });

  var prevBtn = document.getElementById("prev-page");
  if (prevBtn) {
    prevBtn.addEventListener("click", function () {
      if (currentPage > 1) goPage(currentPage - 1);
    });
  }

  var nextBtn = document.getElementById("next-page");
  if (nextBtn) {
    nextBtn.addEventListener("click", function () {
      goPage(currentPage + 1);
    });
  }

  if (searchInput) {
    searchInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") doSearch();
    });
  }

  var searchBtn = document.getElementById("search-btn");
  if (searchBtn) searchBtn.addEventListener("click", doSearch);

  document.getElementById("batch-rows").addEventListener("click", function (e) {
    var target = e.target;
    if (target.tagName === "A" && target.hasAttribute("data-code")) {
      e.preventDefault();
      var code = target.getAttribute("data-code");
      if (navigator.clipboard) {
        navigator.clipboard.writeText(code);
      }
      if (window.showToast) showToast("\u5DF2\u590D\u5236\u9A8C\u8BC1\u7801\uFF1A" + code);
      return;
    }
    var cell = target.closest(".cell-subject");
    if (!cell) return;
    var msgId = cell.getAttribute("data-id");
    if (!msgId || msgId === "0" || msgId === "") return;
    fetch("/admin/batch/message/" + msgId, { headers: { "X-Requested-With": "fetch" } })
      .then(function (res) {
        if (!res.ok) throw new Error();
        return res.json();
      })
      .then(function (msg) {
        document.getElementById("dialog-subject").textContent = msg.subject || "(无主题)";
        var meta = document.getElementById("dialog-meta");
        meta.innerHTML = '<span>\u53D1\u4EF6\u4EBA: ' + esc(msg.sender_email || msg.sender || "-") + '</span><span>\u6536\u4EF6\u4EBA: ' + esc(msg.recipient) + '</span><span>\u65F6\u95F4: ' + esc(msg.time_text) + '</span>';
        var bodyHtml = "";
        if (msg.html_body) {
          bodyHtml = msg.html_body;
        } else {
          bodyHtml = '<pre style="font-size:13px;padding:16px;white-space:pre-wrap;word-break:break-all;margin:0">' + esc(msg.text_body || "") + '</pre>';
        }
        setDialogBody(bodyHtml);
        showMessageDialog();
      })
      .catch(function () {
        if (window.showToast) showToast("\u52A0\u8F7D\u90AE\u4EF6\u5931\u8D25");
      });
  });

  document.getElementById("dialog-close").addEventListener("click", function () {
    closeDialog(document.getElementById("message-dialog"));
  });

  document.getElementById("message-dialog").addEventListener("click", function (e) {
    if (e.target === this) closeDialog(this);
  });
});
