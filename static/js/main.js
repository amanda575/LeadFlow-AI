/* LeadFlow AI — dashboard front-end helpers. */
(function () {
  "use strict";

  function csrfToken() {
    var el = document.querySelector('meta[name="csrf-token"]');
    return el ? el.getAttribute("content") : "";
  }

  // Live template preview (templates page + ad-hoc editor).
  function wireTemplatePreview() {
    var btn = document.getElementById("preview-btn");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var form = document.getElementById("template-form");
      var data = new FormData(form);
      fetch("/api/template/preview", {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken() },
        body: data,
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          var subj = document.getElementById("preview-subject");
          if (subj) subj.textContent = res.subject || "(no subject)";
          var frame = document.getElementById("preview-frame");
          if (frame) {
            var doc = frame.contentDocument || frame.contentWindow.document;
            doc.open();
            doc.write(res.html || "<em>(empty)</em>");
            doc.close();
          }
          var text = document.getElementById("preview-text");
          if (text) text.textContent = res.text || "";
        })
        .catch(function () {});
    });
  }

  // Load a saved template into the editor form.
  function wireTemplateLoad() {
    document.querySelectorAll("[data-load-template]").forEach(function (el) {
      el.addEventListener("click", function () {
        var src = JSON.parse(el.getAttribute("data-load-template"));
        ["name", "subject", "description"].forEach(function (k) {
          var f = document.querySelector('[name="' + k + '"]');
          if (f) f.value = src[k] || "";
        });
        var html = document.querySelector('[name="html_body"]');
        if (html) html.value = src.html_body || "";
        var text = document.querySelector('[name="text_body"]');
        if (text) text.value = src.text_body || "";
        window.scrollTo({ top: 0, behavior: "smooth" });
      });
    });
  }

  // Confirm destructive actions.
  function wireConfirms() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (!window.confirm(form.getAttribute("data-confirm"))) {
          e.preventDefault();
        }
      });
    });
  }

  // Auto-refresh health page every 30s.
  function wireHealthRefresh() {
    if (!document.getElementById("health-grid")) return;
    setInterval(function () {
      fetch("/api/health")
        .then(function (r) { return r.json(); })
        .then(function (h) {
          [["database_ok", "h-db"], ["scheduler_running", "h-sched"],
           ["smtp_ok", "h-smtp"], ["gmail_ok", "h-gmail"]].forEach(function (pair) {
            var dot = document.getElementById(pair[1]);
            if (dot) dot.className = "health-dot " + (h[pair[0]] ? "health-ok" : "health-bad");
          });
        })
        .catch(function () {});
    }, 30000);
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireTemplatePreview();
    wireTemplateLoad();
    wireConfirms();
    wireHealthRefresh();
  });

  // Expose chart bootstrapping for the statistics page.
  window.LeadFlowChart = function (canvasId, labels, sent, replied) {
    var ctx = document.getElementById(canvasId);
    if (!ctx || typeof Chart === "undefined") return;
    new Chart(ctx, {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          { label: "Sent", data: sent, borderColor: "#6366f1", tension: 0.3, fill: false },
          { label: "Replied", data: replied, borderColor: "#22c55e", tension: 0.3, fill: false },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { labels: { color: "#8b96b0" } } },
        scales: {
          x: { ticks: { color: "#8b96b0" }, grid: { color: "rgba(255,255,255,.05)" } },
          y: { ticks: { color: "#8b96b0" }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true },
        },
      },
    });
  };
})();
