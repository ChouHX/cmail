document.addEventListener("DOMContentLoaded", function () {
  var listView = document.getElementById("list-view");
  var detailView = document.getElementById("detail-view");
  var detailBody = document.getElementById("detail-body");
  var metaEl = document.getElementById("inbox-meta");
  var refreshBtn = document.getElementById("refresh");
  var pagination = document.getElementById("pagination");
  var currentId = null;
  var currentPage = 1;

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function rows() {
    return Array.prototype.slice.call(document.querySelectorAll("#list-view [data-message]"));
  }

  function details() {
    return Array.prototype.slice.call(document.querySelectorAll("#detail-body [data-detail]"));
  }

  function showList() {
    currentId = null;
    listView.classList.remove("hidden");
    detailView.classList.add("hidden");
    details().forEach(function (detail) {
      detail.classList.add("hidden");
    });
  }

  function showMessage(id) {
    currentId = id;
    listView.classList.add("hidden");
    detailView.classList.remove("hidden");
    details().forEach(function (detail) {
      detail.classList.toggle("hidden", detail.getAttribute("data-detail") !== id);
    });
  }

  function move(step) {
    if (!currentId) return;
    var list = rows();
    var index = list.findIndex(function (row) {
      return row.getAttribute("data-message") === currentId;
    });
    var next = list[index + step];
    if (next) showMessage(next.getAttribute("data-message"));
  }

  function buildRow(m) {
    var tr = document.createElement("tr");
    tr.className = "mail-row cursor-pointer hover:bg-zinc-50" + (m.unread ? " unread" : "");
    tr.setAttribute("data-message", "m-" + m.id);
    tr.tabIndex = 0;
    var senderClass = m.unread ? "font-semibold text-zinc-900" : "text-zinc-700";
    var subjectClass = m.unread ? "font-medium text-zinc-800" : "text-zinc-600";
    var previewHtml = "";
    if (m.preview) {
      previewHtml = '<span class="text-xs text-zinc-400"> - ' + esc(m.preview) + "</span>";
    }
    tr.innerHTML =
      '<td class="px-4 py-2.5">' +
        '<div class="flex flex-nowrap items-center gap-2 min-w-0">' +
          '<span class="' + senderClass + ' text-sm truncate shrink-0 w-32 sm:w-44" title="' + esc(m.sender) + '">' + esc(m.sender || "-") + '</span>' +
          '<span class="flex-1 min-w-0 truncate">' +
            '<span class="' + subjectClass + ' text-sm">' + esc(m.subject || "(无主题)") + '</span>' +
            previewHtml +
          '</span>' +
          '<span class="text-xs text-zinc-400 whitespace-nowrap shrink-0 text-right">' + esc(m.time_text) + '</span>' +
        '</div>' +
      '</td>';
    return tr;
  }

  function buildDetail(m) {
    var art = document.createElement("article");
    art.className = "message-detail hidden";
    art.setAttribute("data-detail", "m-" + m.id);
    var body;
    if (m.html_body) {
      body = '<div class="bg-white px-4 py-5"><iframe class="min-h-[600px] w-full border-0 bg-white" sandbox></iframe></div>';
    } else {
      body = '<div class="bg-white px-4 py-5"><pre class="whitespace-pre-wrap font-sans leading-7 text-zinc-800">' + esc(m.text_body || "（没有纯文本正文）") + "</pre></div>";
    }
    art.innerHTML =
      '<header class="border-b border-zinc-200 px-4 py-4">' +
        '<h2 class="text-base font-semibold text-zinc-900">' + esc(m.subject) + "</h2>" +
        '<div class="mt-3 grid gap-0.5 text-xs text-zinc-500">' +
          '<p>发件人：<span class="text-zinc-700">' + esc(m.sender || "-") + "</span></p>" +
          "<p>收件人：" + esc(m.recipient) + "</p>" +
          "<p>时间：" + esc(m.time_text) + "</p>" +
        "</div>" +
      "</header>" + body;
    if (m.html_body) {
      art.querySelector("iframe").srcdoc = m.html_body;
    }
    return art;
  }

  function renderPage(data) {
    currentPage = data.page;
    var tbody = document.getElementById("message-rows");
    tbody.innerHTML = "";
    if (!data.messages.length) {
      tbody.innerHTML = '<tr><td class="py-12 text-center text-zinc-400">没有邮件</td></tr>';
    } else {
      data.messages.forEach(function (m) {
        tbody.appendChild(buildRow(m));
        detailBody.appendChild(buildDetail(m));
      });
    }
    pagination.dataset.page = data.page;
    pagination.dataset.pages = data.pages;
    pagination.querySelector("[data-meta]").textContent = "共 " + data.total + " 封 · 第 " + data.page + " / " + data.pages + " 页";
    pagination.querySelector('[data-page-nav="prev"]').disabled = data.page <= 1;
    pagination.querySelector('[data-page-nav="next"]').disabled = data.page >= data.pages;
    if (metaEl) {
      metaEl.textContent = metaEl.getAttribute("data-email") + (data.unread ? " · " + data.unread + " 未读" : "");
    }
    showList();
  }

  function loadPage(page) {
    var token = pagination.dataset.token;
    fetch("/pickup/" + token + "/messages?page=" + page, { headers: { "X-Requested-With": "fetch" } })
      .then(function (res) { return res.json(); })
      .then(renderPage)
      .catch(function () {});
  }

  function silentRefresh() {
    if (document.hidden) return;
    if (currentPage !== 1) return;
    if (!detailView.classList.contains("hidden")) return;
    var token = pagination.dataset.token;
    fetch("/pickup/" + token + "/messages?page=1", { headers: { "X-Requested-With": "fetch" } })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        pagination.dataset.pages = data.pages;
        pagination.querySelector("[data-meta]").textContent = "共 " + data.total + " 封 · 第 " + data.page + " / " + data.pages + " 页";
        pagination.querySelector('[data-page-nav="prev"]').disabled = data.page <= 1;
        pagination.querySelector('[data-page-nav="next"]').disabled = data.page >= data.pages;
        if (metaEl) {
          metaEl.textContent = metaEl.getAttribute("data-email") + (data.unread ? " · " + data.unread + " 未读" : "");
        }
        var tbody = document.getElementById("message-rows");
        var toAdd = [];
        data.messages.forEach(function (m) {
          var id = "m-" + m.id;
          if (!tbody.querySelector('[data-message="' + id + '"]') && !detailBody.querySelector('[data-detail="' + id + '"]')) {
            toAdd.push(m);
          }
        });
        toAdd.reverse().forEach(function (m) {
          tbody.insertBefore(buildRow(m), tbody.firstChild);
          detailBody.insertBefore(buildDetail(m), detailBody.firstChild);
        });
        var max = data.messages.length;
        while (tbody.children.length > max) {
          var last = tbody.lastElementChild;
          if (last && last.hasAttribute("data-message")) {
            var lid = last.getAttribute("data-message");
            var ld = detailBody.querySelector('[data-detail="' + lid + '"]');
            if (ld) ld.remove();
            last.remove();
          } else {
            break;
          }
        }
      })
      .catch(function () {});
  }

  setInterval(silentRefresh, 15000);

  document.addEventListener("click", function (event) {
    var nav = event.target.closest("[data-page-nav]");
    if (nav) {
      var p = parseInt(pagination.dataset.page, 10) || 1;
      loadPage(nav.getAttribute("data-page-nav") === "prev" ? Math.max(1, p - 1) : p + 1);
      return;
    }
    var row = event.target.closest("[data-message]");
    if (row && listView.contains(row)) {
      showMessage(row.getAttribute("data-message"));
    }
  });

  document.addEventListener("keydown", function (event) {
    var row = event.target.closest && event.target.closest("[data-message]");
    if (row && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      showMessage(row.getAttribute("data-message"));
    }
  });

  var back = document.getElementById("back");
  var prev = document.getElementById("prev");
  var next = document.getElementById("next");
  if (back) back.addEventListener("click", showList);
  if (prev) prev.addEventListener("click", function () { move(-1); });
  if (next) next.addEventListener("click", function () { move(1); });

  if (refreshBtn) {
    refreshBtn.addEventListener("click", function () {
      refreshBtn.disabled = true;
      loadPage(currentPage);
      setTimeout(function () { refreshBtn.disabled = false; }, 500);
    });
  }
});
