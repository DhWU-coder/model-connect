"use strict";

// 在样式表加载前恢复主题，避免深色模式下先闪现浅色页面。
(() => {
  const storageKey = "model-connect-theme";
  const choices = ["system", "light", "dark"];
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";

  try {
    const saved = localStorage.getItem(storageKey);
    if (choices.includes(saved)) preference = saved;
  } catch {
    // 浏览器禁止存储时仍能正常显示和切换主题。
  }

  function applyTheme() {
    document.documentElement.dataset.theme =
      preference === "system"
        ? systemTheme.matches
          ? "dark"
          : "light"
        : preference;
  }

  applyTheme();
  systemTheme.addEventListener("change", () => {
    if (preference === "system") applyTheme();
  });

  document.addEventListener("DOMContentLoaded", () => {
    const select = document.getElementById("themeSelect");
    select.value = preference;
    select.addEventListener("change", () => {
      if (!choices.includes(select.value)) return;
      preference = select.value;
      applyTheme();
      try {
        // 只保存显示偏好，不保存密钥或连接配置。
        localStorage.setItem(storageKey, preference);
      } catch {
        // 无法保存时保留当前页面的选择，不影响其他操作。
      }
    });
  });
})();
