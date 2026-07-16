function showToast(message) {
  var toast = document.getElementById("toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "toast";
    toast.className = "fixed bottom-5 left-1/2 z-50 -translate-x-1/2 rounded-md bg-zinc-900 px-3 py-2 text-sm text-white shadow-lg opacity-0 transition-opacity duration-200";
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.remove("opacity-0");
  toast.classList.add("opacity-100");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(function () {
    toast.classList.remove("opacity-100");
    toast.classList.add("opacity-0");
  }, 1800);
}

document.addEventListener("DOMContentLoaded", function () {
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    var area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(area);
    return Promise.resolve();
  }

  document.querySelectorAll(".copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      copyText(btn.getAttribute("data-copy")).finally(function () {
        showToast("已复制");
      });
    });
  });

  document.querySelectorAll(".copy-textarea").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var el = document.getElementById(btn.getAttribute("data-target"));
      if (el) copyText(el.value).finally(function () { showToast("已复制"); });
    });
  });

  document.querySelectorAll(".download-textarea").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var el = document.getElementById(btn.getAttribute("data-target"));
      if (!el) return;
      var blob = new Blob([el.value], { type: "text/plain" });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url;
      a.download = btn.getAttribute("data-filename") || "download.txt";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    });
  });

  var domainSelect = document.getElementById("domain-select");
  if (domainSelect) {
    domainSelect.addEventListener("change", function () {
      var url = domainSelect.value;
      if (url) window.location.href = url;
    });
  }

  var selectAll = document.getElementById("select-all");
  if (selectAll) {
    selectAll.addEventListener("change", function () {
      document.querySelectorAll(".row-check").forEach(function (box) {
        box.checked = selectAll.checked;
      });
    });
  }
});

function closeDialog(dialog) {
  if (typeof dialog.showModal === "function") {
    dialog.classList.add("closing");
    dialog.addEventListener("animationend", function handler() {
      dialog.removeEventListener("animationend", handler);
      dialog.classList.remove("closing");
      dialog.close();
    });
  } else {
    dialog.removeAttribute("open");
  }
}

function openDialog(dialog) {
  dialog.classList.remove("closing");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

document.addEventListener("DOMContentLoaded", function () {
  var navToggle = document.getElementById("nav-toggle");
  var navDropdown = document.getElementById("nav-dropdown");
  if (navToggle && navDropdown) {
    navToggle.addEventListener("click", function () {
      var hidden = navDropdown.classList.toggle("hidden");
      if (!hidden) navDropdown.classList.add("nav-dropdown-open");
    });
    document.addEventListener("click", function (e) {
      if (!navToggle.contains(e.target) && !navDropdown.contains(e.target)) {
        navDropdown.classList.add("hidden");
      }
    });
  }

  document.querySelectorAll("dialog").forEach(function (dialog) {
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) closeDialog(dialog);
    });
  });

  document.querySelectorAll("[data-close-dialog]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var dialog = btn.closest("dialog");
      if (dialog) closeDialog(dialog);
    });
  });

  document.querySelectorAll("[data-dialog]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var name = btn.getAttribute("data-dialog");
      var dialog = document.getElementById("dialog-" + name);
      if (!dialog) return;
      var userId = btn.getAttribute("data-user-id");
      if (userId) {
        var form = dialog.querySelector("form");
        if (form) form.action = "/admin/users/" + userId + "/" + name;
        var quotaInput = dialog.querySelector("[data-quota-input]");
        if (quotaInput) quotaInput.value = btn.getAttribute("data-quota") || "0";
        var nameSpan = dialog.querySelector("[data-delete-name]");
        if (nameSpan) nameSpan.textContent = btn.getAttribute("data-username") || "";
      }
      openDialog(dialog);
    });
  });

  var passwordDialog = document.getElementById("dialog-password");
  if (passwordDialog && passwordDialog.getAttribute("data-password")) {
    var valueEl = passwordDialog.querySelector("[data-password-value]");
    if (valueEl) valueEl.textContent = passwordDialog.getAttribute("data-password");
    openDialog(passwordDialog);
  }
});
