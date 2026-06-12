(function () {
    const THEME_STORAGE_KEY = "aotu-theme";
    const STYLE_STORAGE_KEY = "aotu-style";
    const DEFAULT_STYLE_ID = "jade";
    const STYLE_PRESETS = Object.freeze([
        { id: "jade", name: "玉阶绿", shortDescription: "默认控制台风格", description: "冷静通透，适合日常运维与管理台。", swatches: ["#10b981", "#14b8a6", "#84cc16"] },
        { id: "ocean", name: "深海蓝", shortDescription: "稳重数据风格", description: "深海蓝与海雾青组合，更偏向数据中心气质。", swatches: ["#0f766e", "#0284c7", "#38bdf8"] },
        { id: "amber", name: "琥珀砂", shortDescription: "暖调运营风格", description: "金棕与蜜柑色强调活力和可见度。", swatches: ["#d97706", "#f59e0b", "#fb7185"] },
        { id: "rose", name: "绯雾粉", shortDescription: "柔和品牌风格", description: "偏柔和的玫瑰与珊瑚色，适合更轻盈的界面。", swatches: ["#e11d48", "#fb7185", "#f97316"] },
        { id: "cobalt", name: "钴光蓝", shortDescription: "高对比科技风格", description: "钴蓝与电青更锐利，适合强调技术感。", swatches: ["#2563eb", "#4f46e5", "#06b6d4"] },
        { id: "plum", name: "暮莓紫", shortDescription: "深邃夜幕风格", description: "莓紫与酒红做低饱和混合，更偏夜间工作流。", swatches: ["#7c3aed", "#a855f7", "#ec4899"] },
        { id: "graphite", name: "石墨灰", shortDescription: "极简中性风格", description: "压低色彩表达，保留清爽的工业感和秩序感。", swatches: ["#334155", "#475569", "#94a3b8"] },
        { id: "forest", name: "松林墨", shortDescription: "沉稳自然风格", description: "深松绿与苔藓黄组合，更有自然质感。", swatches: ["#166534", "#15803d", "#a3a948"] },
        { id: "sunset", name: "落日橙", shortDescription: "鲜明增长风格", description: "日落橙与暖红渐变，更强调增长和行动感。", swatches: ["#ea580c", "#f97316", "#ef4444"] },
        { id: "mist", name: "雾屿青", shortDescription: "轻雾冷调风格", description: "灰青与冰蓝更柔和，适合长时间阅读与筛选。", swatches: ["#0f766e", "#14b8a6", "#64748b"] },
    ]);
    const STYLE_PRESET_MAP = new Map(STYLE_PRESETS.map((preset) => [preset.id, preset]));

    function getStoredThemePreference() {
        const storedTheme = window.localStorage.getItem(THEME_STORAGE_KEY);
        return storedTheme === "light" || storedTheme === "dark" ? storedTheme : null;
    }

    function getResolvedTheme() {
        return getStoredThemePreference() || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    }

    function getStoredStylePresetId() {
        const storedStyle = window.localStorage.getItem(STYLE_STORAGE_KEY);
        return STYLE_PRESET_MAP.has(storedStyle) ? storedStyle : null;
    }

    function getResolvedStylePresetId() {
        return getStoredStylePresetId() || DEFAULT_STYLE_ID;
    }

    function getCurrentTheme() {
        return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
    }

    function getCurrentStylePresetId() {
        const currentStyle = document.documentElement.dataset.style;
        return STYLE_PRESET_MAP.has(currentStyle) ? currentStyle : DEFAULT_STYLE_ID;
    }

    function getStylePreset(styleId = getCurrentStylePresetId()) {
        return STYLE_PRESET_MAP.get(styleId) || STYLE_PRESET_MAP.get(DEFAULT_STYLE_ID);
    }

    function renderAppearancePresetGrid(activeStyleId = getCurrentStylePresetId()) {
        const grid = document.getElementById("appearance-preset-grid");
        if (!grid) return;
        grid.innerHTML = STYLE_PRESETS.map((preset) => {
            const isActive = preset.id === activeStyleId;
            const swatches = preset.swatches
                .map((color) => `<span class="appearance-preset-dot" style="--appearance-preset-color:${color}"></span>`)
                .join("");
            return `
                <button
                    class="appearance-preset-card interactive-btn ${isActive ? "is-active" : ""}"
                    type="button"
                    data-style-preset="${preset.id}"
                    aria-pressed="${isActive ? "true" : "false"}"
                >
                    <span class="appearance-preset-swatch" aria-hidden="true">${swatches}</span>
                    <span class="appearance-preset-copy">
                        <strong>${preset.name}</strong>
                        <small>${preset.description}</small>
                    </span>
                    <span class="appearance-preset-state">${isActive ? "当前风格" : "一键应用"}</span>
                </button>
            `;
        }).join("");
    }

    function syncAppearanceUI() {
        const theme = getCurrentTheme();
        const preset = getStylePreset();
        const themeLabel = document.getElementById("theme-toggle-label");
        const themeButton = document.getElementById("theme-toggle");
        const themeIcon = themeButton?.querySelector("i");
        const styleButton = document.getElementById("style-toggle");
        const styleLabel = document.getElementById("style-toggle-label");
        const styleMeta = document.getElementById("style-toggle-meta");
        const currentMode = document.getElementById("appearance-current-mode");
        const currentStyle = document.getElementById("appearance-current-style");

        if (themeLabel) themeLabel.textContent = theme === "dark" ? "切换日间" : "切换暗黑";
        if (themeButton) {
            themeButton.setAttribute("aria-label", theme === "dark" ? "切换到日间模式" : "切换到暗黑模式");
            themeButton.dataset.theme = theme;
        }
        if (themeIcon) themeIcon.className = theme === "dark" ? "bi bi-sun" : "bi bi-moon-stars";
        if (styleButton) {
            styleButton.setAttribute("aria-label", `打开全站风格选择器，当前风格 ${preset.name}`);
            styleButton.dataset.style = preset.id;
        }
        if (styleLabel) styleLabel.textContent = preset.name;
        if (styleMeta) styleMeta.textContent = preset.shortDescription;
        if (currentMode) currentMode.textContent = theme === "dark" ? "暗黑模式" : "日间模式";
        if (currentStyle) currentStyle.textContent = preset.name;
        renderAppearancePresetGrid(preset.id);
    }

    function applyTheme(theme) {
        const nextTheme = theme === "dark" ? "dark" : "light";
        document.documentElement.dataset.theme = nextTheme;
        document.documentElement.style.colorScheme = nextTheme;
        syncAppearanceUI();
    }

    function applyStylePreset(styleId) {
        document.documentElement.dataset.style = STYLE_PRESET_MAP.has(styleId) ? styleId : DEFAULT_STYLE_ID;
        syncAppearanceUI();
    }

    function showToast(message) {
        const stack = document.getElementById("toast-stack");
        if (!stack || !message) return;
        const toast = document.createElement("div");
        toast.className = "toast-message success";
        toast.textContent = message;
        stack.appendChild(toast);
        window.setTimeout(() => toast.remove(), 2200);
    }

    function openAppearanceModal() {
        const modal = document.getElementById("appearance-modal");
        if (!modal) return;
        renderAppearancePresetGrid();
        modal.classList.remove("hidden");
        modal.setAttribute("aria-hidden", "false");
        document.body.classList.add("modal-open");
        window.requestAnimationFrame(() => {
            const focusTarget = modal.querySelector("[data-style-preset][aria-pressed='true']") || document.getElementById("appearance-modal-close");
            focusTarget?.focus?.();
        });
    }

    function closeAppearanceModal() {
        const modal = document.getElementById("appearance-modal");
        if (!modal) return;
        modal.classList.add("hidden");
        modal.setAttribute("aria-hidden", "true");
        document.body.classList.remove("modal-open");
        document.getElementById("style-toggle")?.focus?.();
    }

    function initAppearanceControls() {
        applyTheme(getResolvedTheme());
        applyStylePreset(getResolvedStylePresetId());
        document.getElementById("theme-toggle")?.addEventListener("click", () => {
            const nextTheme = getCurrentTheme() === "dark" ? "light" : "dark";
            window.localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
            applyTheme(nextTheme);
        });
        document.getElementById("style-toggle")?.addEventListener("click", openAppearanceModal);
        document.getElementById("appearance-modal-close")?.addEventListener("click", closeAppearanceModal);
        document.getElementById("appearance-modal")?.addEventListener("click", (event) => {
            if (event.target === event.currentTarget) closeAppearanceModal();
        });
        document.getElementById("appearance-preset-grid")?.addEventListener("click", (event) => {
            const button = event.target.closest("[data-style-preset]");
            if (!button) return;
            const preset = getStylePreset(button.dataset.stylePreset);
            window.localStorage.setItem(STYLE_STORAGE_KEY, preset.id);
            applyStylePreset(preset.id);
            showToast(`已切换为${preset.name}`);
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") closeAppearanceModal();
        });
        const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
        const syncSystemTheme = (event) => {
            if (getStoredThemePreference()) return;
            applyTheme(event.matches ? "dark" : "light");
        };
        if (typeof mediaQuery.addEventListener === "function") {
            mediaQuery.addEventListener("change", syncSystemTheme);
        } else if (typeof mediaQuery.addListener === "function") {
            mediaQuery.addListener(syncSystemTheme);
        }
    }

    document.addEventListener("DOMContentLoaded", initAppearanceControls);
})();
